# VoxInk Remaining Work

Status: Active roadmap

## Purpose

This is the maintained list of work that remains after the developer workflow
and native capture integration. Completed implementation plans are removed once
their durable requirements are reflected here, in `README.md`, or in tests.

`PROJECT_SPEC.md` remains the architecture and data-format reference.
`SHIP_SPEC.md` remains the specification for a signed production release.

## Documentation cleanup

Goal: keep only Markdown that is current, useful, and has a clear owner.

Steps:

- [x] Inventory tracked Markdown and find inbound references.
- [x] Separate durable specifications from completed task plans.
- [x] Remove completed task plans and repair documentation links.
- [x] Verify the remaining Markdown set and commit the cleanup.

Completion criteria:

- Completed one-off plans are absent from the repository.
- The README links to each maintained specification or roadmap.
- This file contains only unfinished product work and durable release gates.
- Markdown links resolve and the worktree is clean after the cleanup commit.

## Current state

The repository currently provides:

- A one-command browser workflow through `./scripts/voxink-dev`.
- Local-only Qwen transcription over authenticated loopback HTTP/WebSocket APIs.
- Microphone, all-system, selected-application, and combined capture modes.
- Browser controls for start, pause, resume, and stop.
- Source-labelled live captions and rotating Markdown transcripts.
- Optional separate and combined rotating WAV retention.
- Bounded native audio queues, UI event history, and helper diagnostics.
- Recovery of interrupted transcript parts without overwriting earlier output.
- Python, Swift, and frontend automated tests.

The native helper builds and its automated tests pass. Real system-audio
delivery still requires acceptance on the target Mac because the last manual
check received zero-filled ScreenCaptureKit buffers despite successful
permission and stream setup.

## Current priorities

### Completed slice: Default audio retention controls

Goal: make local audio retention visible and enabled by default in both the
browser and Terminal developer workflows without changing the existing WAV
persistence format.

Steps:

- [x] Default the launcher to retain audio and provide an explicit opt-out.
- [x] Add a pre-session Save audio control to the browser recorder.
- [x] Pass the selected setting through microphone and native capture starts.
- [x] Update focused launcher and frontend tests and user documentation.

Completion criteria:

- Browser and Terminal launches retain separate rotating WAV tracks by default.
- `--no-retain-audio` disables retention for either interface.
- The browser control is enabled before capture and locked during a session.
- Focused Python and frontend validation passes.

Result: completed and verified with the full Python suite, frontend lint,
TypeScript checking, a production static build, and browser interaction testing.

### P0: Target-Mac capture acceptance

- Repeat the native system-audio test with known audible applications.
- Confirm microphone, all-system, selected-application, and combined sessions.
- Record permission state, audio delivery, source labels, and saved output.
- If ScreenCaptureKit still supplies silence, isolate the OS/framework cause or
  add a replaceable capture backend without weakening local-only behavior.

Complete when every capture mode delivers real audio end to end on a supported
Mac, or a documented platform limitation has a tested fallback.

### P0: Durable session discovery and recovery

- Add a versioned session manifest or small local registry.
- Persist lifecycle state, enabled sources, model/protocol versions, and part
  inventory without secrets or transcript text.
- Discover interrupted sessions without scanning Markdown contents.
- Add transcript and transcript-folder actions.
- Add explicit low-disk behavior and retention reporting.

Complete when interrupted and completed sessions can be found, resumed safely,
and opened without overwriting prior transcript or audio parts.

### P0: Reliability validation

- Add deterministic replay capture for end-to-end failure tests.
- Exercise helper failure, slow inference, malformed traffic, model corruption,
  low disk, output removal, and network-disabled operation.
- Add accelerated one-hour replay and synthetic 24-hour soak coverage.
- Verify bounded memory, queues, diagnostics, and temporary storage.

Complete when finalized text survives tested failures and long runs stay
bounded with no unexpected outbound traffic.

### P1: Complete developer controls

- Add microphone-device selection.
- Add open-transcript, open-folder, and quit actions.
- Improve queue, device, permission, and recovery messages.
- Show model/provider identity and inference lag in the browser status surface.

Complete when the full session lifecycle and its common recovery actions are
available without direct API or Terminal coordination.

### P1: Responsive partial captions and performance

- Build a reproducible English, Mandarin, and code-switching evaluation set.
- Measure first-visible-text, silence-to-final latency, real-time factor,
  memory, and transcript quality.
- Evaluate rolling inference and Apple Silicon acceleration behind the existing
  provider interface.
- Emit revision-safe partial events while keeping partial text out of Markdown.

Complete when revisions replace one another without reordering or duplication,
and the selected local runtime meets a documented target on supported hardware.

### P2: Local macOS application

- Add a Finder-launchable menu-bar application that reuses the existing capture,
  service, caption, and session interfaces.
- Include source selection, permissions, settings, diagnostics, and transcript
  actions.
- Assemble and ad-hoc sign the local development app.

Complete when the local app can finish all supported capture modes without a
Terminal. Developer ID signing, notarization, installation, and updates remain
production work governed by `SHIP_SPEC.md`.

## Developer-edition gates

The developer edition is ready when:

1. One command reaches a ready state without manual port or helper management.
2. All advertised capture modes pass target-Mac acceptance.
3. Local IPC remains authenticated, versioned, and loopback-only.
4. Finalized transcripts survive interruption and recovery without overwrite.
5. Slow inference and long sessions keep memory and storage bounded.
6. Offline tests detect no unexpected network access.
7. Model, permission, device, helper, and disk failures have useful recovery
   actions.
8. Python, Swift, frontend, real-model smoke, replay, and soak checks pass in
   the supported development environment.
