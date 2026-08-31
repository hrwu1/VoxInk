# VoxInk Remaining Work

Status: Active roadmap. Last verified against the tree on 2026-08-31.

## Purpose

This is the maintained list of work that remains after the developer workflow
and native capture integration. Completed implementation plans are removed once
their durable requirements are reflected here, in `README.md`, or in tests; the
Git history keeps their detail.

`PROJECT_SPEC.md` remains the architecture and data-format reference.
`SHIP_SPEC.md` remains the specification for a signed production release.

## Verified state

The following was confirmed by reading the tree and running the checks, not
from memory of earlier work:

- A one-command browser workflow through `./scripts/voxink-dev`, on the
  preferred loopback port `8765` with an automatic fallback.
- A public `./scripts/voxink` command for scripts and agents, speaking a
  versioned JSON envelope over the helper's local control API.
- One capture path: every browser and CLI mode records through the native
  helper. No `getUserMedia`, `AudioContext`, or client-side resampling remains
  in `frontend/app`. `clients/python_capture` is kept as an independent
  diagnostic client, but its VAD module is not diagnostic-only; see below.
- Local-only Qwen transcription over authenticated loopback HTTP/WebSocket
  APIs, with model listing and selection in the browser.
- Microphone, all-system, selected-application, and combined capture modes,
  with start, pause, resume, and stop from both the browser and the CLI.
- Source-labelled live captions, rotating Markdown transcripts, and rotating
  WAV retention enabled by default with an explicit opt-out.
- Bounded native audio queues, UI event history, and helper diagnostics.
- Recovery of interrupted transcript parts without overwriting earlier output.

Checks, with their results on 2026-08-31:

- `.venv/bin/python -m unittest discover -s tests` — 267 tests, all passing.
- `swift test` in `clients/macos_capture` — 13 tests, all passing.
- The frontend has **no automated test runner**. `frontend/package.json`
  defines `lint`, `build`, and `build:local` only. Frontend confidence today
  rests on ESLint, TypeScript checking, a production build, and manual browser
  interaction. This gap is tracked under reliability validation below.

Real system-audio delivery still requires acceptance on the target Mac: the
last manual check received zero-filled ScreenCaptureKit buffers despite
successful permission and stream setup.

## Current priorities

### P0: Level-adaptive voice activity detection

This is first because it degrades every session that runs today, and the fix
does not depend on any other item.

The energy VAD gates on a fixed absolute threshold that is hardcoded in two
places, both on the live capture path:

- `clients/python_capture/vad.py:21` — `threshold_rms: float = 500.0`, the
  default used by the chunker.
- `clients/macos_capture/Sources/VoxInkCapture/LocalServiceClient.swift:365` —
  `let thresholdRMS = 500.0`, sent to the service in the session start message.

The module lives under `clients/python_capture`, but it is **not** confined to
that diagnostic client: `services/transcription/app.py` imports
`EnergyVadChunker` and applies it to every session, using the threshold the
native helper supplies. Changing only one of the two constants changes nothing
for real sessions.

A fixed absolute threshold cannot be right across microphones and input gains,
and it now drops words. Measured on a real microphone-only session: the
retained WAV peaks at -19.8 dBFS and only 6.2% of its 20 ms frames clear the
threshold. The live transcript read "This is just a test for." / "To see
whether you have captured."; the same WAV transcribed as one file read "This is
just a test for to see whether you have captured all the words I've said
about." The words are in the recording and the model reads them — the VAD
discards them as silence.

The flaw was latent while the browser captured microphone audio itself, because
`getUserMedia` applied automatic gain control that lifted the signal to meet
the threshold. The native helper applies none: `PCMNormalizer` converts format,
not level, and no gain stage exists anywhere in the Swift sources. Unifying
microphone capture removed the gain that was masking this.

- [ ] Derive the threshold from a running noise floor instead of a constant.
- [ ] Decide where the threshold is owned. Two hardcoded constants that must
      agree is the defect underneath the defect; the service should derive its
      own gate rather than trust a client-supplied absolute level.
- [ ] Consider a gain stage in the native helper as well; automatic gain helps
      the model, not only the VAD.
- [ ] Verify against a retained WAV with known ground truth before changing the
      live path, and keep that WAV as a regression fixture.

Complete when quiet speech at roughly -20 dBFS survives capture, the threshold
no longer depends on one client's preprocessing, and a test fails if the gate
regresses to a fixed absolute level.

### P0: Target-Mac capture acceptance

- [ ] Repeat the native system-audio test with known audible applications.
- [ ] Confirm microphone, all-system, selected-application, and combined
      sessions.
- [ ] Record permission state, audio delivery, source labels, and saved output.
- [ ] If ScreenCaptureKit still supplies silence, isolate the OS/framework
      cause or add a replaceable capture backend without weakening local-only
      behavior.

Microphone capture is confirmed working end to end through the native helper,
so it is the control case: a failure that affects both sources is not
ScreenCaptureKit-specific. Run this after the VAD work, so that dropped words
are not mistaken for a delivery failure.

Complete when every capture mode delivers real audio end to end on a supported
Mac, or a documented platform limitation has a tested fallback.

### P0: Durable session discovery and recovery

Nothing here is started. `_sessions_list` in `voxink_cli/cli.py:373` returns a
hardcoded `"durable_registry": False` and warns that only sessions the running
helper holds open are listable.

- [ ] Add a versioned session manifest or small local registry.
- [ ] Back `voxink sessions list` with it so completed and interrupted sessions
      are listable, and add the revision and `--dry-run` behavior that depends
      on it.
- [ ] Persist lifecycle state, enabled sources, model and protocol versions,
      and part inventory without secrets or transcript text.
- [ ] Discover interrupted sessions without scanning Markdown contents.
- [ ] Add transcript and transcript-folder actions.
- [ ] Add explicit low-disk behavior and retention reporting.
- [ ] Extract a domain capture route. `capture start` still reaches the control
      plane through `/v1/ui/capture/*`, a UI-namespaced route, deferred here on
      purpose.

Complete when interrupted and completed sessions can be found, resumed safely,
and opened without overwriting prior transcript or audio parts.

### P0: Reliability validation

Nothing here is started. There is no replay or soak coverage in `tests/` or
`voxink_dev/`, and `benchmarks/` holds four static result files only.

- [ ] Add deterministic replay capture for end-to-end failure tests.
- [ ] Exercise helper failure, slow inference, malformed traffic, model
      corruption, low disk, output removal, and network-disabled operation.
- [ ] Add accelerated one-hour replay and synthetic 24-hour soak coverage.
- [ ] Verify bounded memory, queues, diagnostics, and temporary storage.
- [ ] Add a frontend test runner. The recorder hook now owns the whole capture
      lifecycle for every surface, and it has no automated coverage at all.

Complete when finalized text survives tested failures, long runs stay bounded
with no unexpected outbound traffic, and every layer that can break a session
has a test that runs without a human.

### P1: Complete developer controls

Partly done. The browser shows model identity and offers model selection
(`frontend/app/page.tsx:114`). What remains:

- [ ] Add microphone-device selection. `/v1/devices` lists input devices
      (`services/transcription/app.py:501`), but `/v1/ui/capture/start` accepts
      only sources, applications, and retention — there is no device parameter
      to pass one through, in either the browser or the CLI.
- [ ] Add open-transcript, open-folder, and quit actions.
- [ ] Wire or remove the two inert browser controls: the settings button
      (`page.tsx:48`) has no handler, and `Mark` (`page.tsx:186`) is
      permanently disabled.
- [ ] Improve queue, device, permission, and recovery messages.
- [ ] Show inference lag in the browser status surface.

Complete when the full session lifecycle and its common recovery actions are
available without direct API or Terminal coordination, and no control in the
interface does nothing.

### P1: Responsive partial captions and performance

Not started. `services/transcription/app.py:262` advertises
`"partial_results": False`, and no revision plumbing exists.

- [ ] Build a reproducible English, Mandarin, and code-switching evaluation
      set.
- [ ] Measure first-visible-text, silence-to-final latency, real-time factor,
      memory, and transcript quality.
- [ ] Evaluate rolling inference and Apple Silicon acceleration behind the
      existing provider interface.
- [ ] Emit revision-safe partial events while keeping partial text out of
      Markdown.

Complete when revisions replace one another without reordering or duplication,
and the selected local runtime meets a documented target on supported hardware.

### P1: Remaining public CLI surface

Deferred when the first command slice shipped; recorded here so the deferral
stays visible. `README.md` documents the accepted deviations, and
`.claude/plans/public-cli.md` records the review behind them.

- [ ] Add the `audio`, `config`, `diagnostics`, and `permissions` command
      groups from `PROJECT_SPEC.md` section 14.3.
- [ ] Add transcript search and export, and derived agent artifacts.

Scoped grants stay unenforced in this edition by design: possession of the
loopback credential authorizes every scope, and `capabilities` says so. Real
grants are signed-application work governed by `SHIP_SPEC.md`, and no CLI flag
may bypass that gate.

Complete when section 14.3 is either implemented or explicitly narrowed in
`PROJECT_SPEC.md`.

### P2: Local macOS application

Not started. The Swift sources build a capture CLI only; there is no
`NSStatusItem` or `MenuBarExtra` anywhere in the tree.

- [ ] Add a Finder-launchable menu-bar application that reuses the existing
      capture, service, caption, and session interfaces.
- [ ] Include source selection, permissions, settings, diagnostics, and
      transcript actions.
- [ ] Assemble and ad-hoc sign the local development app.

Complete when the local app can finish all supported capture modes without a
Terminal. Developer ID signing, notarization, installation, and updates remain
production work governed by `SHIP_SPEC.md`.

## Developer-edition gates

The developer edition is ready when:

1. One command reaches a ready state without manual port or helper management.
2. All advertised capture modes pass target-Mac acceptance.
3. Capture preserves quiet speech across microphones and input gains.
4. Local IPC remains authenticated, versioned, and loopback-only.
5. Finalized transcripts survive interruption and recovery without overwrite.
6. Slow inference and long sessions keep memory and storage bounded.
7. Offline tests detect no unexpected network access.
8. Model, permission, device, helper, and disk failures have useful recovery
   actions.
9. Python, Swift, frontend, real-model smoke, replay, and soak checks pass in
   the supported development environment.

Gate 1 is met. Gate 4 is met. Gate 5 is met for transcript parts and covered by
tests. Gates 2, 3, 6, 7, 8, and 9 are open, and gate 9 additionally requires a
frontend test runner that does not yet exist.
