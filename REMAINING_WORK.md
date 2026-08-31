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
- A public `./scripts/voxink` command for scripts and agents, speaking a
  versioned JSON envelope over the helper's local control API.
- One capture path: every browser and CLI mode records through the native
  helper, with `clients/python_capture` kept as an independent diagnostic.
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

### Completed slice: Stable preferred developer port

Goal: make the supervised developer workflow use the familiar loopback port
`8765` when available without reintroducing manual port conflict handling.

Steps:

- [x] Prefer `127.0.0.1:8765` when starting the supervised helper.
- [x] Fall back to an operating-system-assigned loopback port when `8765` is
  unavailable.
- [x] Document the behavior and add focused port-selection tests.

Completion criteria:

- A normal launch uses port `8765`.
- A launch still succeeds without user intervention when port `8765` is busy.
- Focused supervisor tests and the full Python test suite pass.

Result: completed and verified with 8 focused supervisor tests and all 187
Python tests.

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

### Completed slice: Public `voxink` command

Goal: implement the first command slice of `PROJECT_SPEC.md` section 14 as a
thin client of the local control plane, with a versioned envelope, stable error
codes, and stable exit codes.

Steps:

- [x] Publish a private control-endpoint handshake from the helper's owner and
  discover it from the CLI.
- [x] Add the service routes the CLI needs so it never scans the output root:
  session listing, input devices, and transcript discovery and reading.
- [x] Add `voxink_cli` with the envelope, error and exit taxonomy, typed
  control client, and the first command slice.
- [x] Add `scripts/voxink` and document the public contract.

Completion criteria:

- Every command emits the section 14.4 envelope in `json`, and a stable error
  code and exit code on failure.
- Every command reports `not_ready` rather than a traceback with no helper.
- The CLI reaches every capability through the control API only.
- The Python test suite passes.

Result: completed and verified with 77 new Python tests, the full 264-test
Python suite, and a live check against the real Qwen helper covering
readiness, discovery, model listing, device listing, file transcription,
transcript retrieval, and vocabulary replacement.

Deferred with the durable session registry: expected revisions and `--dry-run`.
Deferred from section 14.3: the `audio`, `config`, `diagnostics`, and
`permissions` groups, transcript search and export, and derived artifacts.
Scoped grants are not enforced until the signed application can issue them; see
`.claude/plans/public-cli.md` for the reviewed deviations.

### Completed slice: Unified microphone capture

Goal: give every surface one way to reach the microphone, replacing three
microphone implementations and two ways to control a capture client.

Steps:

- [x] Let the control plane start microphone-only native capture.
- [x] Route every browser capture mode through that one path.
- [x] Drop the CLI's microphone-only special case.
- [x] Update the Python, service, and CLI tests for one capture path.

Completion criteria:

- Microphone-only capture starts from both the browser and the CLI.
- No `getUserMedia`, `AudioContext`, or client-side resampling remains in the
  frontend.
- `capabilities` no longer advertises a mode that `capture start` refuses.
- Python and frontend checks pass, verified in the real application.

Result: completed. Verified by driving the real application: microphone-only
browser capture transcribed live speech end to end, pause/resume/stop worked,
and all five capture modes produced the correct helper arguments under one
rule. The public CLI drove the same lifecycle against the same helper.

The `/v1/ui/capture/*` route is still UI-namespaced; extracting a domain
capture route is deferred with the durable session registry below.

### P0: Target-Mac capture acceptance

- Repeat the native system-audio test with known audible applications.
- Microphone capture is now confirmed working end to end through the native
  helper, so it is the control case: a failure that affects both sources is
  not ScreenCaptureKit-specific.
- Confirm microphone, all-system, selected-application, and combined sessions.
- Record permission state, audio delivery, source labels, and saved output.
- If ScreenCaptureKit still supplies silence, isolate the OS/framework cause or
  add a replaceable capture backend without weakening local-only behavior.

Complete when every capture mode delivers real audio end to end on a supported
Mac, or a documented platform limitation has a tested fallback.

### P0: Durable session discovery and recovery

- Add a versioned session manifest or small local registry.
- Back `voxink sessions list` with it so completed and interrupted sessions are
  listable, and add the revision and `--dry-run` behavior that depends on it.
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
