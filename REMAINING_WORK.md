# VoxInk Remaining Work

Status: Developer-edition implementation plan
Last updated: 2026-08-25

## Purpose

VoxInk already has a working local transcription prototype: Qwen inference,
localhost HTTP and WebSocket APIs, microphone and macOS system-audio capture,
selected-application filtering, source-labeled Markdown, optional audio
retention, VAD finalization, and a basic floating caption panel.

The next target is a cohesive developer edition that works as one program even
before Xcode, Developer ID signing, notarization, and public distribution are
available. A user of the repository should not need to start several processes,
choose ports, send `curl` requests, or coordinate multiple Terminal windows.

`SHIP_SPEC.md` remains the specification for the eventual signed production
release. This document describes the code that can be completed now and the
small amount of native packaging work that remains toolchain-dependent.

## Developer-Edition Goal

One command, provisionally:

```sh
./scripts/voxink-dev
```

must:

1. Validate the Python runtime, dependencies, model files, output location,
   permissions, and available disk space.
2. Start the local transcription helper automatically.
3. Wait for the helper to become ready and report model-loading progress.
4. Let the user select microphone, all-system, or selected-application audio.
5. Start, pause, resume, and stop a session.
6. Display live source-labeled captions and useful status information.
7. Shut down capture and transcription cleanly.
8. Report and optionally open the saved transcript.
9. Detect and recover an interrupted session without overwriting files.

The developer edition may use a terminal interface and ad-hoc local app
packaging. It does not need Developer ID signing or notarization.

## Current Constraints

- The Python service and tests run in the repository's Python 3.12 virtual
  environment.
- The local Qwen3-ASR-1.7B model occupies approximately 4.4 GB.
- The native capture code exists as a Swift package and command-line tool, not
  as a complete macOS application.
- The installed Command Line Tools currently have a Swift compiler/macOS SDK
  mismatch, so native builds cannot be verified until the tools are aligned.
- A paid Apple Developer account is not required for local development or
  ad-hoc signing. It is required later for Developer ID signing and notarized
  distribution.

Code and tests that do not require compiling Swift should proceed immediately.
Swift source can also be developed now, but native acceptance tests remain
blocked until a matching Swift toolchain is available.

## Priority Overview

| Priority | Milestone | Outcome |
| --- | --- | --- |
| P0 | One-command supervisor | One process owns startup, readiness, capture, and shutdown |
| P0 | Authenticated local protocol | Other local processes cannot control or observe VoxInk |
| P0 | Durable session registry | Interrupted work is recoverable and sessions are discoverable |
| P0 | Unified developer controls | Sources and the full session lifecycle are controlled in one place |
| P0 | Reliability harness | Crashes, slow inference, offline use, and long sessions are tested |
| P1 | Responsive partial captions | Replaceable text appears before finalization |
| P1 | Ad-hoc local macOS app | Finder-launchable developer app without public signing |
| Deferred | Production distribution | Xcode release build, Developer ID, notarization, DMG, and updates |

## Milestone 1: One-Command Local Supervisor

### Goal

Replace manual service and capture startup with one process that owns the whole
developer workflow.

### Work

- Add a `voxink_dev` Python package and a small `scripts/voxink-dev` entrypoint.
- Add preflight checks for:
  - Python version and required imports.
  - Complete local model directory and expected model files.
  - Model manifest and cryptographic checksums.
  - Writable transcript and log directories.
  - Available disk space, including the cost of audio retention.
  - Native capture executable availability when system audio is selected.
- Choose an unused loopback port rather than assuming port 8765 is free.
- Start the transcription service as a supervised child process.
- Wait for health, protocol compatibility, and readiness before enabling Start.
- Report model-loading progress and readable failures.
- Start the selected capture client with the correct session configuration.
- Monitor child processes and distinguish clean exit from unexpected failure.
- Restart the helper only when recovery is safe.
- Forward termination signals and flush finalized work before exit.
- Store structured local diagnostics without transcript text, raw audio, secrets,
  or unnecessary sensitive paths.

### Acceptance criteria

- One command reaches a ready state from a configured checkout.
- No manual server startup, port choice, `curl`, or second Terminal is required.
- Missing dependencies or model files are reported before capture begins.
- Stop, Quit, and Ctrl-C close capture, helper, transcript, and audio files.
- An unexpected helper exit is visible and never silently discards finalized
  transcript text.
- Microphone-only operation works even when the native capture executable is
  unavailable.

### Suggested commits

1. `feat: add developer supervisor and preflight checks`
2. `feat: supervise helper readiness and shutdown`
3. `feat: add private structured diagnostics`

## Milestone 2: Authenticated and Versioned Local Protocol

### Goal

Keep the local API convenient for development while preventing unrelated local
processes from controlling sessions or reading transcript events.

### Work

- Generate a cryptographically random secret for every supervisor launch.
- Pass the secret to the helper through its environment, not command-line
  arguments or persistent configuration.
- Require authentication for session HTTP routes and WebSocket upgrades.
- Send authentication from Python and Swift clients using request headers.
- Add a service, protocol, and client compatibility handshake.
- Define provider capability fields such as partial-result support.
- Enforce size limits for JSON controls, audio headers, and binary frames.
- Strictly validate message type, schema version, source, stream, sequence,
  timing, and declared byte length.
- Cancel pending work when a stream or session stops.
- Keep `/health` lightweight; expose only the minimum unauthenticated status
  required for supervision, or authenticate it as well.
- Add tests proving missing or incorrect credentials cannot control or observe a
  session.

### Acceptance criteria

- A process without the per-launch secret cannot create sessions, send audio,
  stop capture, or receive transcripts.
- Credentials never appear in process arguments, transcript metadata, or logs.
- Unsupported protocol versions fail before audio capture begins.
- Oversized and malformed messages are rejected without killing healthy
  sessions.
- Existing Python and Swift contract fixtures cover the same schema.

### Suggested commits

1. `feat: authenticate local helper communication`
2. `feat: add protocol compatibility negotiation`
3. `test: harden local message validation and limits`

## Milestone 3: Durable Session Registry and Recovery

### Goal

Make session state independent of in-memory objects and Markdown front matter so
an app or helper crash can be recovered safely.

### Work

- Add a versioned `session.json` manifest or a small SQLite registry.
- Persist:
  - Session ID, lifecycle state, and start/end timestamps.
  - Enabled sources, selected applications, and language preference.
  - Provider/model versions and protocol version.
  - Transcript and audio part inventory.
  - Whether the session stopped normally or requires recovery.
- Update lifecycle state atomically.
- On startup, detect incomplete sessions and mark them recovered.
- Continue with new monotonically numbered parts; never overwrite an existing
  transcript or audio file.
- List sessions without scanning Markdown contents.
- Add commands to open a transcript or its folder.
- Add explicit deletion semantics for transcript, audio, and application-managed
  metadata.
- Check disk space before writes fail.
- Disable audio retention safely under low-disk conditions while preserving
  transcript integrity where possible.
- Add configurable retention policies and disk-usage reporting.

### Acceptance criteria

- Killing the supervisor or helper cannot corrupt or overwrite finalized text.
- The next launch discovers and recovers an interrupted session.
- Completed, recovered, and multi-part sessions can all be listed and opened.
- Low disk space produces an actionable error and does not silently lose text.
- Session metadata contains no secrets.

### Suggested commits

1. `feat: persist normalized session manifests`
2. `feat: recover interrupted sessions without overwrite`
3. `feat: add session listing and disk safeguards`

## Milestone 4: Unified Developer Controls

### Goal

Provide one coherent interface for configuration, source selection, capture,
captions, and transcript access.

### Work

- Add an interactive terminal interface with clear states:
  - Not ready.
  - Ready.
  - Starting.
  - Recording.
  - Paused.
  - Stopping.
  - Failed.
- Provide microphone selection.
- Provide system-audio selection:
  - None.
  - All system audio.
  - One or more running applications.
- Prevent invalid source combinations before Start.
- Expose Start, Pause, Resume, Stop, Open, and Quit commands.
- Show elapsed time, Local-only status, active model/provider, queue lag, and
  dropped-frame count.
- Render microphone and system captions in chronological order with textual
  source labels.
- Replace partial captions by stable segment ID rather than appending duplicates.
- Print and optionally open the transcript when a session stops.
- Translate permission, device, selected-application, model, disk, and helper
  failures into user-readable messages with a next action.
- Extend the Swift development CLI with interactive pause/resume if the native
  helper remains a separate process.

### Acceptance criteria

- A user can complete a microphone session without knowing the service API.
- When the Swift executable is available, the same workflow supports all-system,
  selected-application, and simultaneous microphone/system capture.
- Every lifecycle state is visible and invalid transitions are blocked.
- Captions and saved Markdown retain source identity.
- Selected applications disappearing and returning do not require a new logical
  session.

### Suggested commits

1. `feat: add unified developer session controls`
2. `feat: integrate source selection and live status`
3. `feat: add transcript discovery and readable errors`

## Milestone 5: Reliability and Test Harness

### Goal

Prove that the developer edition remains bounded and preserves finalized work
during realistic failure and long-session conditions.

### Work

- Add subprocess end-to-end tests for supervisor, helper, and fake capture.
- Add a deterministic replay capture source for real-time and accelerated tests.
- Test helper crash, restart, stop, Ctrl-C, and forced termination.
- Test malformed messages, wrong credentials, and protocol mismatch.
- Test inference slower than capture.
- Bound all audio, transcript, inference, and diagnostics queues.
- Expose queue depth, dropped frames, and lag without logging transcript content.
- Test network-disabled local operation and monitor for unexpected outbound
  traffic.
- Test missing/corrupt models and partial model installation.
- Test Markdown rotation at six hours and 5 MiB.
- Test WAV rotation at one hour and the safe RIFF byte limit.
- Test combined audio generation across rotated parts.
- Test low disk, failed writes, and output-directory removal.
- Add one-hour replay tests and a synthetic 24-hour soak test.
- When native builds are available, test device changes, sleep/wake, permission
  removal, and selected-application exit/restart on real hardware.

### Acceptance criteria

- A 24-hour synthetic run has bounded memory and temporary storage.
- Slow inference never creates an unbounded queue.
- Finalized transcript text survives every tested process failure.
- Local-only mode works with network access disabled and produces no unexpected
  outbound traffic.
- Rotation and recovery never overwrite an existing part.
- Native device/source loss is visible while unaffected sources continue.

### Suggested commits

1. `test: add supervised end-to-end replay harness`
2. `test: cover crash recovery and network isolation`
3. `test: add long-session and queue-pressure soak tests`
4. `feat: handle low disk and native source changes`

## Milestone 6: Performance and Partial Captions

### Goal

Improve perceived responsiveness while keeping final transcript output stable
and durable.

### Work

- Build a reproducible evaluation set covering:
  - English and Mandarin.
  - English/Chinese code-switching.
  - Short utterances and long meetings.
  - Silence, noise, music, and overlapping sources.
  - Microphone and system-audio recordings.
- Record CPU model-load time, first-result latency, finalization latency,
  real-time factor, memory, and transcript quality.
- Benchmark rolling-window inference using the current Qwen model.
- Once available, compare MPS, MLX, and a smaller model profile.
- Add provider capability negotiation for partial and streaming support.
- Assign one stable segment ID to every revision of an active utterance.
- Emit ordered `transcript.partial` events and exactly one durable
  `transcript.final` event.
- Cancel obsolete rolling inference when a newer revision supersedes it.
- Keep partial text out of Markdown.
- Measure first-visible-text and silence-to-final latency separately.
- Detect and report when inference falls behind real time.

### Acceptance criteria

- Partial revisions replace each other and never create duplicate Markdown.
- Slow inference cannot reorder transcript events.
- Pause and Stop deterministically cancel or finalize the active utterance.
- The selected default model/runtime meets a documented real-time target on the
  supported development machine.
- Model/runtime changes are compared against the recorded bilingual baseline.

### Suggested commits

1. `bench: add bilingual accuracy and latency baseline`
2. `feat: add provider capability negotiation`
3. `feat: emit revision-safe partial transcripts`
4. `test: cover partial ordering and cancellation`

## Milestone 7: Ad-Hoc Local macOS Application

### Goal

Provide a Finder-launchable developer app without requiring a paid developer
account or public distribution credentials.

### Dependency

This milestone requires a matching Swift compiler and macOS SDK. Full Xcode is
not conceptually required, but the current Command Line Tools mismatch must be
fixed before the result can be compiled or verified.

### Work

- Add a SwiftPM menu-bar executable target using SwiftUI and AppKit.
- Reuse `VoxInkCapture`, `CaptureCoordinator`, `LocalServiceClient`, and the
  floating caption panel.
- Integrate the developer supervisor and the same state machine used by the
  terminal workflow.
- Add source selection, permissions, session controls, settings, diagnostics,
  and transcript-folder actions.
- Assemble a local `.app` bundle from the SwiftPM release build.
- Include `Info.plist`, usage descriptions, entitlements, icons, and helper
  resources.
- Ad-hoc sign local executables and the app with `codesign --sign -`.
- Document that rebuilds may reset macOS privacy grants because the app lacks a
  stable Developer ID identity.

### Acceptance criteria

- The local app launches from Finder without manually starting the helper.
- It completes microphone, system, selected-application, and dual-source
  sessions using the same persisted session contract as the terminal workflow.
- Permission denial produces recovery guidance rather than a crash.
- All bundled executables are covered by local ad-hoc signing.
- The limitations of the non-notarized developer build are documented clearly.

### Suggested commits

1. `feat: add SwiftPM developer menu-bar app`
2. `feat: integrate supervisor and native session controls`
3. `build: assemble and ad-hoc sign local app bundle`

## Deferred Until Production Distribution

The following are not required to make the developer edition work as a whole:

- Developer ID signing and stable signing identities.
- Apple notarization and stapling.
- A polished DMG installer.
- An authenticated automatic-update channel.
- App Store distribution.
- Production crash reporting or support upload workflows.
- Speaker diarization.
- Forced-alignment word timestamps.
- Online transcription providers.
- Windows and Linux capture adapters.
- Direct typing into other applications.

These remain governed by `SHIP_SPEC.md` and should start only after the local
developer edition passes its reliability and privacy gates.

## Recommended Implementation Sequence

1. Add the one-command supervisor and preflight checks.
2. Authenticate and version all local helper communication.
3. Add durable session manifests and interrupted-session recovery.
4. Build the unified terminal controls and transcript discovery workflow.
5. Add subprocess, crash, offline, queue-pressure, rotation, and soak tests.
6. Establish the bilingual CPU baseline and implement partial captions.
7. Repair the Swift toolchain and verify the existing native package.
8. Build and ad-hoc sign the local menu-bar application.
9. Return to `SHIP_SPEC.md` for production runtime packaging, Developer ID
   signing, notarization, installation, and updates.

## Developer-Edition Release Gates

The developer edition is workable as a whole when all of the following are
true:

1. One command starts VoxInk and reaches a ready state.
2. No manual helper, port, WebSocket, or HTTP management is required.
3. Microphone capture completes an end-to-end session in every checkout.
4. System and selected-application capture work whenever the native helper is
   available.
5. Start, Pause, Resume, Stop, captions, and transcript access share one
   coherent interface.
6. Local IPC is authenticated and versioned.
7. Model, permission, device, helper, and disk failures have readable recovery
   actions.
8. Finalized transcript text survives forced termination and recovery.
9. All queues and temporary storage remain bounded under slow inference.
10. Local-only mode works with network access disabled.
11. Automated Python tests, protocol contract tests, real-model smoke tests,
    replay integration tests, and the synthetic soak test pass.
12. Native tests pass once a matching Swift toolchain is available.

## First Implementation Slice

The first focused slice should deliver:

- `scripts/voxink-dev` and its Python supervisor package.
- Model, dependency, output-directory, and disk preflight checks.
- Automatic helper startup on a private loopback port.
- Readiness waiting, readable status, signal forwarding, and clean shutdown.
- A per-launch authentication secret shared with the helper.
- A microphone-only end-to-end test proving that one command produces a saved
  transcript.

This slice provides immediate value without Xcode and creates the lifecycle
boundary that both the terminal interface and eventual native application will
reuse.
