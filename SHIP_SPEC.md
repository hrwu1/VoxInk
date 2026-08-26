# VoxInk Shipping Specification

Status: Proposed

Last updated: 2026-08-25

## 1. Shipping Goal

Ship VoxInk as a signed, notarized macOS menu-bar application that can be
installed and used without a terminal or a developer environment.

A user must be able to install VoxInk on a clean supported Mac, complete
permission and model setup, select microphone and system-audio sources, start a
session, see responsive captions, stop the session, and open the saved
transcript from the application.

The existing capture, transcription, session, and persistence components are
the foundation of the product. The shipping effort packages those components
behind a coherent application, adds production lifecycle management, and meets
the performance, privacy, reliability, and distribution requirements below.

## 2. Initial Product Scope

The first production release targets Apple Silicon Macs running macOS 13 or
newer.

It supports:

- Local-only speech recognition.
- Microphone-only capture.
- All-system-audio capture.
- Selected-application audio capture.
- Simultaneous microphone and system capture with separate source labels.
- English and Chinese transcription with automatic detection or an explicit
  language preference.
- Floating captions.
- Markdown transcript persistence.
- Optional separate and combined audio retention.
- Manually controlled start, pause, resume, and stop.

The following are not release blockers for version 1:

- Speaker diarization or speaker identification.
- Cloud transcription providers.
- Windows or Linux support.
- Guaranteed word-level timestamps.
- Direct typing into other applications.
- A mobile application.

## 3. Product Experience

### 3.1 Installation and first launch

The user installs and launches one signed application. VoxInk must not require
Homebrew, Python, Xcode, command-line tools, or manually entered shell commands.

First-run setup must:

1. Explain that transcription runs locally.
2. Request microphone permission when microphone capture is selected.
3. Request Screen & System Audio Recording permission when system capture is
   selected.
4. Explain when macOS requires the application to restart after a permission
   change.
5. Locate, validate, import, or install the transcription model and runtime.
6. Show required disk space and installation progress.
7. Confirm readiness or present a recoverable error.

The user may import a verified offline model package. If VoxInk offers a model
download, the download must be explicit and must not weaken offline operation
after setup.

### 3.2 Menu-bar application

The menu-bar interface must provide:

- Current state: not ready, ready, starting, recording, paused, stopping, or
  failed.
- Microphone selection.
- System-audio selection: none, all system audio, or one or more running
  applications.
- Start, Pause, Resume, and Stop controls.
- A visible recording indicator and elapsed time.
- Caption visibility and click-through controls.
- Transcript destination selection.
- Audio-retention controls with clear storage implications.
- Language selection with an automatic option.
- Open Current Transcript and Open Transcript Folder actions.
- Access to settings, diagnostics, help, and Quit.

Invalid combinations must be prevented in the UI instead of being reported
only after capture starts.

### 3.3 Floating captions

The floating caption window must:

- Stay above ordinary windows.
- Support an optional click-through mode.
- Show microphone and system captions in chronological order.
- Identify each source using text in addition to color.
- Replace partial text without duplicating it.
- Preserve finalized text in the session transcript.
- Remain legible across light and dark appearances and common display scales.
- Remember its position and user-selected appearance.

### 3.4 Errors and recovery

Errors must be expressed in user language and include a useful next action.
Expected cases include permission denial, missing model data, insufficient disk
space, unavailable audio devices, selected applications not running, helper
failure, and transcription falling behind real time.

Detailed diagnostics may be available separately, but raw exceptions and
protocol messages must not be the primary user experience.

## 4. Application Architecture

### 4.1 Native application

Create an Xcode application target that links the existing `VoxInkCapture`
Swift package. The application owns the menu-bar UI, settings, permission flow,
capture selection, caption presentation, and session lifecycle.

The development CLI may remain for diagnostics, but it is not part of the
normal product flow.

### 4.2 Transcription helper

The application must automatically start, monitor, and stop the local
transcription helper. The helper lifecycle must include:

- A readiness probe that does not load the model unnecessarily.
- A version and protocol compatibility handshake.
- Model-loading progress and failure reporting.
- Automatic restart after an unexpected failure when recovery is safe.
- Graceful shutdown and final transcript flushing.
- Bounded audio queues and explicit backpressure behavior.
- Cancellation of obsolete or stopped-session inference work.

Users must not manage a virtual environment, dependencies, ports, or helper
processes.

### 4.3 Runtime and model packaging

Before the beta release, benchmark at least the existing Python/Torch runtime
and one viable accelerated Apple Silicon path. Select the production runtime
using measured accuracy, latency, memory, power usage, package size, model
compatibility, and maintenance cost.

If Python remains in the first release, its interpreter and dependencies must
be self-contained, reproducible, signed, and installed by VoxInk. VoxInk must
never depend on a system Python installation.

The model and runtime may be installed separately from the application bundle
under the user's Application Support directory. Installation must support:

- Resumable progress where downloads are offered.
- Cryptographic checksum validation.
- Versioned model storage and safe migration.
- Detection of partial or corrupted installations.
- Disk-space validation before installation.
- Removal from the application's storage settings.

### 4.4 Local protocol security

Binding to loopback is required but is not sufficient authentication. The
shipping application must prevent arbitrary local processes from controlling
sessions or reading transcript events.

Use authenticated local communication with a per-launch secret, a Unix domain
socket, XPC, or an equivalently private mechanism. Credentials must not be
placed in command-line arguments or persistent logs.

Protocol messages must remain versioned, size-limited, and strictly validated.

## 5. Transcription Requirements

### 5.1 Responsiveness

The production path must be benchmarked on every supported hardware tier. The
release must define and meet targets for:

- Model startup time.
- Time from speech to the first visible text.
- Time from detected silence to finalized text.
- Real-time factor during continuous use.
- Peak and steady-state memory.
- CPU/GPU usage and battery impact.

Final threshold values must be set from representative-device measurements
before beta. The system must detect when inference is falling behind and must
not accumulate an unbounded queue.

### 5.2 Partial and final text

The current VAD-finalized path is acceptable as a fallback, but the intended
caption experience requires replaceable partial text or sufficiently frequent
stable chunks. Partial results are disposable; finalized results are durable.

Chunk overlap and deduplication must not introduce repeated phrases into the
saved transcript.

### 5.3 Accuracy evaluation

Maintain an evaluation set containing:

- English and Mandarin speech.
- Code-switching between English and Chinese.
- Silence, background noise, and music.
- Different microphones and system-audio sources.
- Overlapping microphone and system speech.
- Short utterances and long-form meetings.

Runtime or model changes may ship only after comparison against the recorded
accuracy and latency baseline.

## 6. Reliability Requirements

VoxInk must handle the following without losing already finalized transcript
content:

- Helper process failure.
- Application restart after an interrupted session.
- Sleep and wake.
- Microphone connection, disconnection, or default-device change.
- Selected application exit and restart.
- Screen or microphone permission removal.
- Network unavailability during local-only operation.
- Low disk space or a failed transcript/audio write.
- Malformed or incomplete local protocol messages.
- Stop or Quit while transcription work is pending.

Session state and files must be recoverable after forced termination. Recovery
must never silently overwrite an existing transcript or audio part.

Long-running capture must use bounded memory and bounded temporary storage.

## 7. Privacy and Security Requirements

- Local mode must perform no network requests after installation and must be
  testable with network access disabled.
- Microphone and system audio must never leave the Mac in local mode.
- Telemetry, diagnostics upload, and crash reporting must be absent or
  explicitly opt-in.
- Logs must not contain raw audio, transcript text, authentication material, or
  sensitive filesystem paths by default.
- Transcript and retained-audio files must use user-only permissions.
- Audio retention must be off by default and clearly indicated while enabled.
- The application must disclose where models, transcripts, audio, and logs are
  stored and provide controls to remove application-managed data.
- Dependencies and model artifacts must have documented provenance, licenses,
  integrity checks, and third-party notices.

## 8. Quality and Test Requirements

### 8.1 Automated testing

Continuous integration must use pinned Python and Xcode/Swift environments and
must run:

- Existing Python unit and API tests.
- Swift unit and contract tests.
- Protocol compatibility tests across the app and helper.
- Golden Markdown and event-schema tests.
- Model-installation integrity and migration tests.
- Release-mode builds with signing checks where credentials permit.

### 8.2 Integration testing

Release validation must include:

- A real-model file-to-transcript test.
- End-to-end microphone capture.
- End-to-end all-system and selected-application capture.
- Simultaneous microphone and system capture.
- Permission denial and recovery.
- Helper crash and restart.
- Application restart and transcript recovery.
- Audio-device and selected-application disappearance.
- Offline operation with network traffic monitored.

### 8.3 Soak testing

Before production release, complete representative multi-hour meeting tests
with transcript rotation, optional audio retention, pause/resume, overlapping
sources, sleep/wake, and forced termination. Record memory growth, queue depth,
latency, dropped audio, and recovery outcomes.

## 9. Distribution Requirements

The production release must include:

- A stable application bundle identifier.
- Developer ID signing.
- Hardened runtime configuration.
- Correct microphone and screen/audio capture entitlements and usage strings.
- Apple notarization and stapling.
- A reproducible release archive and checksum.
- A user-friendly DMG or equivalent installer.
- An authenticated update mechanism or a documented manual update path.
- Release notes, privacy documentation, third-party notices, support guidance,
  and uninstall instructions.

The release pipeline must reject unsigned helpers, unverified model artifacts,
incompatible protocol versions, and mismatched build dependencies.

## 10. Delivery Milestones

### Milestone 1: Product shell

- Create the signed macOS app target.
- Add menu-bar state and session controls.
- Add source selection and permission onboarding.
- Integrate the existing caption panel.
- Add settings and transcript-folder actions.

Exit criterion: a developer build can complete a session without terminal
interaction, assuming an already installed helper and model.

### Milestone 2: Managed local engine

- Package or install a self-contained helper runtime.
- Add helper supervision, readiness, versioning, and shutdown.
- Add model installation/import, integrity validation, and progress UI.
- Secure local communication.

Exit criterion: a clean Mac can reach a ready state without development tools.

### Milestone 3: Performance and caption quality

- Select and integrate the production inference runtime.
- Add accelerated Apple Silicon inference.
- Establish supported-device performance targets.
- Add partial or frequent stable caption updates.
- Add queue limits, backpressure, overlap handling, and lag reporting.

Exit criterion: continuous transcription remains responsive and bounded on all
supported hardware.

### Milestone 4: Reliability beta

- Implement recovery paths and user-facing errors.
- Add CI, integration tests, and soak tests.
- Validate offline behavior and privacy guarantees.
- Produce a signed and notarized beta.

Exit criterion: beta users can install, operate, recover, and update VoxInk
without developer assistance.

### Milestone 5: Production release

- Resolve beta-blocking defects.
- Complete licensing, privacy, documentation, and support material.
- Complete final security and performance review.
- Publish the notarized production build and update channel.

Exit criterion: every release gate in the next section passes.

## 11. Production Release Gates

VoxInk is ready to ship only when all of the following are true:

1. A clean supported Mac can install and run VoxInk without a terminal,
   developer tools, or a separately installed Python environment.
2. First-run permissions and model setup succeed or produce recoverable,
   user-readable errors.
3. Microphone, all-system, selected-application, and combined capture work from
   the application UI.
4. Captions remain responsive and transcripts remain durable throughout the
   supported session duration.
5. Inference queues and temporary storage remain bounded when processing is
   slower than capture.
6. Forced termination cannot corrupt or overwrite finalized transcript data.
7. Local-only mode produces no network traffic after setup.
8. Local IPC is authenticated or private to the application.
9. Performance, memory, and power targets pass on every supported hardware
   tier.
10. Automated tests, real-model integration tests, and soak tests pass in the
    pinned release environment.
11. The application and all executable helpers are signed, hardened,
    notarized, and distributed with verified artifacts.
12. Privacy, licensing, third-party notices, support, update, and uninstall
    documentation are complete.

## 12. Immediate Next Work

The next implementation slice should create the macOS application target and a
small application state machine covering not-ready, ready, starting, recording,
paused, stopping, and failed states. It should expose microphone/system source
selection and Start/Stop controls while reusing the current capture library and
caption panel.

In parallel with that product shell, align and pin the Xcode/Swift toolchain so
native builds are reproducible, then benchmark the current CPU inference path
to establish the baseline used for the production runtime decision.
