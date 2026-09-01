# VoxInk Remaining Work

Status: Active roadmap. Last verified against the tree on 2026-08-31.

## Purpose

This is the maintained list of work that remains after the developer workflow
and native capture integration. Completed implementation plans are removed once
their durable requirements are reflected here, in `README.md`, or in tests; the
Git history keeps their detail.

`PROJECT_SPEC.md` remains the architecture and data-format reference.
`SHIP_SPEC.md` remains the specification for a signed production release.

## How this list is ordered

The project is six days old and has one user. Order follows evidence, not the
shape of the specification:

1. Defects that degrade every session run today.
2. Capabilities the interface advertises but cannot deliver.
3. Verification of the code most likely to break next, which is the code that
   changes most and is tested least.
4. Convenience and completeness.

Work with no observed problem behind it belongs in `Deferred` at the end, not
in a priority tier. Two P0 items is a priority; five is a wish list.

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
  diagnostic client, though the service applies its VAD module to every session.
- A speech gate derived from a running noise floor rather than a fixed level,
  owned by the service. No client sends an absolute threshold. Measured on the
  recording that exposed the defect: speech-frame recall rose from 83.2% to
  100%, and silence and room tone from -70 to -35 dBFS still produce nothing.
- Local-only Qwen transcription over authenticated loopback HTTP/WebSocket
  APIs, with model listing and selection in the browser.
- Microphone, all-system, selected-application, and combined capture modes,
  with start, pause, resume, and stop from both the browser and the CLI.
- Source-labelled live captions, rotating Markdown transcripts, and rotating
  WAV retention enabled by default with an explicit opt-out.
- Bounded native audio queues, UI event history, and helper diagnostics.
- Bounded audio retention: a free-space reserve and an optional per-session
  ceiling, enforced in the writer. Reaching either stops the recording, not the
  session, and is reported in the browser, on `voxink capture status`, and as
  retained bytes per session on `voxink transcripts list`. Nothing is pruned
  automatically; see `.claude/plans/bounded-local-storage.md`.
- Recovery of interrupted transcript parts without overwriting earlier output.

Checks, with their results on 2026-08-31:

- `.venv/bin/python -m unittest discover -s tests` — 304 tests, all passing.
- `swift test` in `clients/macos_capture` — 15 tests, all passing.
- `npm run test:timer` in `frontend` — 2 tests, passing. The frontend runs
  Node's built-in test runner under `--experimental-strip-types`, added with
  the session-timer fix. It covers the extracted `elapsed-timer.ts` helper
  only, and its script names that one file rather than discovering tests.
  Everything else in the frontend still rests on ESLint, TypeScript checking,
  a production build, and manual browser interaction.

Real system-audio delivery still requires acceptance on the target Mac: the
last manual check received zero-filled ScreenCaptureKit buffers despite
successful permission and stream setup.

## P0: Target-Mac capture acceptance

An advertised capture mode returns silence. Either it works, or the interface
should stop offering it.

- [ ] Repeat the native system-audio test with known audible applications.
- [ ] Confirm microphone, all-system, selected-application, and combined
      sessions.
- [ ] Record permission state, audio delivery, source labels, and saved output.
- [ ] If ScreenCaptureKit still supplies silence, isolate the OS/framework
      cause or add a replaceable capture backend without weakening local-only
      behavior.

Microphone capture is confirmed working end to end through the native helper,
so it is the control case: a failure that affects both sources is not
ScreenCaptureKit-specific. The VAD work this was sequenced behind is done, so
dropped words can no longer be mistaken for a delivery failure.

Complete when every capture mode delivers real audio end to end on a supported
Mac, or a documented platform limitation has a tested fallback.

## P1: Cover the code that changes most

The recorder hook owns the entire capture lifecycle for every surface. That is
the highest-churn, least-tested code in the repository. The session-timer fix
established the runner and showed the shape that works: extract the logic into
a pure module and test it directly. The remaining lifecycle logic still lives
inside the hook, untested. The heavier reliability infrastructure is deferred;
see the end of this file.

- [ ] Give the runner a discovery script. `test:timer` names a single file, so
      a second test file runs only if someone remembers to add another script.
- [ ] Cover the recorder hook: mode selection, the start/pause/resume/stop
      lifecycle, and error and readiness states. Extract what has to be
      extracted to make it testable, as the timer fix did.
- [ ] Add a focused failure test for the helper dying mid-session. The disk
      filling mid-session is covered by the retention bounds.
- [ ] Exercise malformed traffic and model corruption at the service boundary.

Complete when the capture lifecycle has automated coverage that runs without
being named file by file, and a helper crash or a full disk is a tested path
rather than an assumption.

## P1: Prepare the pyannote Community-1 environment

Goal: make the existing project Python environment capable of running the
local Community-1 diarization pipeline without implementing or advertising
speaker diarization yet.

- [x] Add pinned pyannote, torchaudio, and TorchCodec dependencies in a clearly
      labelled section of `requirements.txt` while retaining the existing Qwen
      and service pins.
- [x] Install the added packages into the existing `.venv` and install FFmpeg
      for TorchCodec audio decoding.
- [x] Verify package versions, dependency consistency, FFmpeg discovery, and a
      basic pyannote import without downloading gated model weights.
- [x] Run the existing Python test suite to detect regressions in the Qwen and
      service environment.

Complete when one `.venv` imports both the existing Qwen stack and
`pyannote.audio`, `pip check` reports no conflicts, FFmpeg is available, and
the existing Python tests still pass. Model download, model execution, speaker
alignment, APIs, and UI work remain separate approval-gated implementation.

Verified on 2026-08-31 with PyTorch 2.13.0, pyannote.audio 4.0.7,
TorchAudio 2.11.0, TorchCodec 0.16.0, and FFmpeg 9.0.1. `pip check` reported no
conflicts, both Qwen and pyannote imports succeeded, TorchCodec decoded a local
16 kHz WAV through pyannote, and all 304 existing Python tests passed.

## P1: Complete developer controls

Partly done. The browser shows model identity and offers model selection
(`frontend/app/page.tsx:114`). What remains:

- [ ] Add microphone-device selection. `/v1/devices` lists input devices
      (`services/transcription/app.py:501`), but `/v1/ui/capture/start` accepts
      only sources, applications, and retention — there is no device parameter
      to pass one through, in either the browser or the CLI. This also
      interacts with the VAD work: different microphones are exactly the
      varying input levels a fixed threshold cannot serve.
- [ ] Add open-transcript, open-folder, and quit actions.
- [ ] Wire or remove the two inert browser controls: the settings button
      (`page.tsx:48`) has no handler, and `Mark` (`page.tsx:186`) is
      permanently disabled.
- [ ] Improve queue, device, permission, and recovery messages.
- [ ] Show inference lag in the browser status surface.

Complete when the full session lifecycle and its common recovery actions are
available without direct API or Terminal coordination, and no control in the
interface does nothing.

## P1: Durable session discovery and recovery

Demoted from P0. The artifacts that matter — transcript parts and audio —
already land on disk and survive interruption, with tests. What is missing is
listing and metadata, and with one user and a flat output directory, `ls` still
answers the question. `_sessions_list` in `voxink_cli/cli.py:373` returns a
hardcoded `"durable_registry": False` and warns that only sessions the running
helper holds open are listable.

Raise this again when session count, multiple machines, or agent use makes
directory listing genuinely insufficient.

- [ ] Add a versioned session manifest or small local registry.
- [ ] Back `voxink sessions list` with it so completed and interrupted sessions
      are listable, and add the revision and `--dry-run` behavior that depends
      on it.
- [ ] Persist lifecycle state, enabled sources, model and protocol versions,
      and part inventory without secrets or transcript text.
- [ ] Discover interrupted sessions without scanning Markdown contents.
- [ ] Add transcript and transcript-folder actions.
- [ ] Extract a domain capture route. `capture start` still reaches the control
      plane through `/v1/ui/capture/*`, a UI-namespaced route, deferred here on
      purpose.

Complete when interrupted and completed sessions can be found, resumed safely,
and opened without overwriting prior transcript or audio parts.

## P1: Responsive partial captions and performance

Not started. `services/transcription/app.py:262` advertises
`"partial_results": False`, and no revision plumbing exists. A genuine
improvement, not a necessity: finalized-only text works. Keep it behind the
capture work.

- [ ] Extend the level fixtures in `tests/fixtures/audio.py` into a
      reproducible English, Mandarin, and code-switching evaluation set.
- [ ] Consider a gain stage in the native helper. Deferred from the VAD work
      deliberately: it changes the audio the model receives and the audio
      retained on disk, which belongs with measuring transcript quality rather
      than with fixing a gate. `PCMNormalizer` converts format, not level.
- [ ] Measure first-visible-text, silence-to-final latency, real-time factor,
      memory, and transcript quality.
- [ ] Evaluate rolling inference and Apple Silicon acceleration behind the
      existing provider interface.
- [ ] Emit revision-safe partial events while keeping partial text out of
      Markdown.

Complete when revisions replace one another without reordering or duplication,
and the selected local runtime meets a documented target on supported hardware.

## P2: Local macOS application

Not started. The Swift sources build a capture CLI only; there is no
`NSStatusItem` or `MenuBarExtra` anywhere in the tree. Plausibly the highest
day-to-day value item here once capture is trustworthy, and deliberately
behind it: a menu-bar app over a VAD that drops words just makes the defect
easier to reach.

- [ ] Add a Finder-launchable menu-bar application that reuses the existing
      capture, service, caption, and session interfaces.
- [ ] Include source selection, permissions, settings, diagnostics, and
      transcript actions.
- [ ] Assemble and ad-hoc sign the local development app.

Complete when the local app can finish all supported capture modes without a
Terminal. Developer ID signing, notarization, installation, and updates remain
production work governed by `SHIP_SPEC.md`.

## Deferred

Not scheduled. Each of these is real work with no observed problem behind it
yet; the trigger that should promote it is named.

**Long-run and replay infrastructure.** Accelerated one-hour replay, synthetic
24-hour soak, and deterministic replay capture. Queues, event history, and
temporary storage are already bounded by construction and covered by tests, so
this would verify a design rather than chase a symptom. Promote on the first
evidence of a leak, or when sessions routinely run for hours.

**Remaining public CLI surface.** The `audio`, `config`, `diagnostics`, and
`permissions` command groups from `PROJECT_SPEC.md` section 14.3, plus
transcript search and export and derived agent artifacts. The first command
slice has one user. Promote when something concrete needs a command that is
missing, rather than to complete the section. `README.md` documents the
accepted deviations, and `.claude/plans/public-cli.md` records the review.

**Scoped grants.** Unenforced by design in this edition: possession of the
loopback credential authorizes every scope, and `capabilities` says so. Real
grants are signed-application work governed by `SHIP_SPEC.md`. No CLI flag may
bypass that gate.

**Offline network assertion.** Verifying no unexpected outbound traffic is a
release gate, cheap to run once the failure tests above exist, and pointless to
build separately before then.

## Developer-edition gates

These describe the finished developer edition. They are a definition of done,
not a work order — the sections above decide sequencing.

1. One command reaches a ready state without manual port or helper management.
2. All advertised capture modes pass target-Mac acceptance.
3. Capture preserves quiet speech across microphones and input gains, and
   silence produces no text. Met for the gate, which is now derived from the
   input and covered by level fixtures; unverified on real hardware until the
   acceptance run above.
4. Local IPC remains authenticated, versioned, and loopback-only.
5. Finalized transcripts survive interruption and recovery without overwrite.
6. Slow inference, long sessions, and a full disk keep memory and storage
   bounded and never cost finalized text. The full-disk half is met and
   covered; slow inference and long sessions are not yet exercised.
7. Offline tests detect no unexpected network access.
8. Model, permission, device, helper, and disk failures have useful recovery
   actions.
9. Python, Swift, frontend, real-model smoke, replay, and soak checks pass in
   the supported development environment.

Met: 1, 4, and 5 — gate 5 for transcript parts, on the strength of its tests
rather than a fresh manual interruption. Gate 3 is met in the code and in
fixtures but not yet on hardware. Open: 2, 6, 7, 8, and 9. Gate 9 now has a
frontend runner, but it covers one helper and must be pointed at each file by
hand.
