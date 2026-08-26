# VoxInk Remaining Work

Status: Post-prototype roadmap
Last updated: 2026-08-25

## Purpose

The working prototype already provides local Qwen transcription, localhost
HTTP/WebSocket APIs, microphone and macOS system-audio capture, multi-application
filtering, source-labeled Markdown, optional audio retention, VAD finalization,
and a basic floating caption panel.

This document describes the work required to turn that prototype into a
reliable desktop product. Items are ordered by dependency and user value. Each
milestone is intended to fit in one or a small number of focused Git commits.

## Priority Overview

| Priority | Milestone | Outcome |
| --- | --- | --- |
| P0 | Native application shell | Signed, launchable macOS menu-bar application |
| P0 | Runtime and model packaging | Reproducible offline installation and startup |
| P0 | Real-device reliability | Stable mic/system capture during long sessions |
| P1 | Incremental partial captions | Text appears before an utterance is finalized |
| P1 | Apple Silicon acceleration | Lower latency and resource consumption |
| P1 | Push-to-talk workflow | Global shortcut with clipboard output |
| P1 | Session browser and settings | Users can find transcripts and configure capture |
| P2 | Accurate timestamps | Word-level timestamps through forced alignment |
| P2 | Speaker diarization | Optional speaker labels without changing the API |
| P2 | Online providers | Explicit opt-in fallback for constrained devices |
| P3 | Windows and Linux | Platform capture adapters using the same protocol |

## Milestone 1: Native macOS Application Shell

### Goal

Turn the Swift capture library and floating panel into a signed macOS
application that users can launch without a terminal.

### Work

- Create an Xcode macOS app target using SwiftUI and AppKit where needed.
- Add a menu-bar item with Start, Pause, Resume, Stop, and Quit controls.
- Embed the existing microphone, ScreenCaptureKit, service-client, and floating
  caption components.
- Provide capture-source selection:
  - Microphone only.
  - All system audio.
  - One or more selected applications.
  - Microphone plus either system mode.
- Add application selection using ScreenCaptureKit shareable-content results.
- Display permission state and actionable recovery instructions.
- Add settings for caption visibility, click-through behavior, transcript
  destination, language mode, and audio retention.
- Display the active provider and an unmistakable Local-only/Online indicator.
- Bundle the existing usage descriptions and entitlements.
- Configure code signing, hardened runtime, and notarization-ready settings.

### Acceptance criteria

- The signed app launches without Terminal.
- The menu-bar controls operate a complete manual session lifecycle.
- Microphone and system permissions are requested with clear explanations.
- The floating caption window remains visible across Spaces and full-screen
  applications.
- Selected applications can quit and restart without manual reselection.
- The app never captures its own audio.
- Denied permissions do not crash the app.

### Suggested commits

1. `feat: add macOS menu-bar application shell`
2. `feat: add native source and permission controls`
3. `feat: integrate floating captions and session settings`
4. `build: configure macOS signing and release metadata`

## Milestone 2: Reproducible Offline Runtime Packaging

### Goal

Install and launch the Python service and local model predictably without
requiring users to manage a development environment.

### Work

- Decide whether the Python service is bundled, installed on first launch, or
  replaced by a native inference runtime.
- Pin all Python, model, and capture dependencies with hashes.
- Add a service supervisor owned by the macOS app.
- Start the service on demand and stop it when no longer needed.
- Wait for `/health` readiness before enabling capture.
- Surface model-loading progress and errors in the app.
- Use a private application-support directory for runtime files.
- Define whether model weights are bundled or installed as a separate offline
  package.
- Validate model-file checksums before loading.
- Ensure startup never attempts to contact package or model registries.
- Add migration logic for future service and model versions.

### Acceptance criteria

- A clean Mac can install and run VoxInk without manually invoking Python or
  `pip`.
- Disconnecting the Mac from the network does not affect startup or
  transcription.
- The app detects missing or corrupt model files before capture begins.
- Service crashes are detected and surfaced without losing existing transcript
  data.
- Uninstall instructions clearly identify retained transcripts and model data.

### Suggested commits

1. `build: pin and lock offline service dependencies`
2. `feat: supervise the local transcription service`
3. `feat: validate and migrate local model assets`
4. `build: package the offline runtime for macOS`

## Milestone 3: Long-Running Reliability

### Goal

Make effectively unlimited sessions safe for real meetings and continuous
caption use.

### Work

- Run microphone-only, system-only, and dual-source soak tests.
- Test selected applications restarting, changing audio devices, sleeping, and
  waking.
- Test headphones and microphones being connected or removed mid-session.
- Bound every in-memory audio and transcript queue.
- Add backpressure and explicit dropped-frame counters.
- Persist session state independently of Markdown headers.
- Recover interrupted sessions after app or service crashes.
- Verify Markdown rotation at six hours and 5 MiB.
- Verify WAV rotation at one hour and the safe RIFF byte cap.
- Verify combined-track generation across all rotated parts.
- Handle low disk space before writes fail.
- Add retention policies for old audio and transcripts.
- Add structured local logs that never contain raw audio or credentials.

### Acceptance criteria

- A 24-hour synthetic session completes without unbounded memory growth.
- A six-hour real or replayed dual-source session produces valid Markdown and
  playable audio parts.
- Killing the UI or service loses no finalized transcript segments.
- Restarting recovers the prior session without overwriting files.
- Source loss is visible to the user and remaining sources continue working.
- Low-disk conditions stop audio retention safely while preserving transcript
  integrity where possible.

### Suggested commits

1. `test: add long-running capture soak harness`
2. `feat: persist and recover active session state`
3. `feat: add bounded queues and capture backpressure`
4. `feat: handle device changes and low disk space`

## Milestone 4: Incremental Partial Transcription

### Goal

Show useful replaceable text while the user is still speaking, then commit a
stable result after VAD finalization.

### Work

- Benchmark Qwen inference on rolling windows.
- Decide between native Qwen streaming support and repeated chunk inference.
- Add provider capabilities such as `supports_partials` and
  `supports_streaming`.
- Assign a stable segment ID to all revisions of one active utterance.
- Emit ordered `transcript.partial` events.
- Replace partial text in the floating panel instead of appending duplicates.
- Emit exactly one `transcript.final` event per finalized utterance.
- Write only finalized events to Markdown.
- Cancel stale inference when a newer partial supersedes it.
- Handle punctuation and language changes between revisions.
- Measure first-text latency and finalization latency separately.

### Acceptance criteria

- Initial caption text normally appears within the chosen latency target.
- Partial revisions never appear as duplicate Markdown paragraphs.
- Final text replaces the corresponding partial using the same segment ID.
- Slow inference cannot reorder transcript events.
- Pausing or stopping flushes or cancels the active partial deterministically.

### Suggested commits

1. `feat: add provider streaming capability negotiation`
2. `feat: emit revision-safe partial transcript events`
3. `feat: render replaceable partial captions`
4. `test: cover partial ordering and cancellation`

## Milestone 5: Apple Silicon Acceleration

### Goal

Reduce latency, memory use, and battery consumption on Apple Silicon.

### Work

- Determine why the current Torch build reports MPS unavailable.
- Test a compatible official PyTorch build with MPS support.
- Benchmark CPU and MPS for model load time, real-time factor, memory, and power.
- Evaluate an MLX-compatible Qwen conversion if MPS is insufficient.
- Evaluate smaller models such as Qwen3-ASR-0.6B.
- Add provider/model profiles such as Accuracy, Balanced, and Low-resource.
- Detect available hardware and select a safe default.
- Add model warm-up and idle-unload policies.
- Prevent model loading from making the caption UI unresponsive.

### Acceptance criteria

- The selected accelerated runtime produces transcripts matching an agreed
  quality baseline.
- Live transcription maintains a real-time factor below 1.0 on supported Macs.
- The application remains responsive while loading and unloading the model.
- Unsupported hardware falls back safely to CPU or a smaller local model.

### Suggested commits

1. `fix: enable supported MPS inference runtime`
2. `bench: compare local Apple Silicon inference backends`
3. `feat: add hardware-aware model profiles`
4. `feat: add model warm-up and idle unloading`

## Milestone 6: Push-to-Talk

### Goal

Provide short dictation through a configurable global shortcut.

### Work

- Register a global press-and-hold shortcut.
- Start microphone capture on key-down and finalize on key-up.
- Show compact recording and processing feedback.
- Copy finalized text to the clipboard by default.
- Optionally append push-to-talk results to the current session Markdown.
- Preserve the prior clipboard value until transcription succeeds.
- Handle shortcut conflicts and secure-input contexts.
- Consider direct typing only as a separate, explicitly permissioned feature.

### Acceptance criteria

- Pressing and holding the shortcut records only during the intended interval.
- Releasing it produces one finalized result and copies it to the clipboard.
- Failed transcription does not overwrite the clipboard.
- The workflow works while another application is focused.
- Accessibility permission is not required for clipboard-only behavior.

### Suggested commits

1. `feat: add configurable global push-to-talk shortcut`
2. `feat: add safe clipboard transcription output`
3. `test: cover push-to-talk lifecycle edge cases`

## Milestone 7: Session Browser and Settings

### Goal

Make saved transcripts discoverable and routine configuration manageable.

### Work

- List prior sessions using metadata rather than scanning Markdown content.
- Show session start, duration, sources, selected apps, language, provider, and
  audio availability.
- Open a transcript or its folder in Finder.
- Add safe deletion with clear audio/transcript scope.
- Persist default capture mode and selected devices.
- Persist language and transcript destination settings.
- Configure Markdown and audio rotation thresholds.
- Add visible disk-usage estimates when audio retention is enabled.
- Add an export path for Markdown and future subtitle formats.

### Acceptance criteria

- Users can find and open every completed session from the app.
- Settings survive relaunch and have documented defaults.
- Deleting a session requires confirmation and identifies exactly what will be
  removed.
- The browser handles recovered and multi-part sessions correctly.

## Milestone 8: Forced Alignment and Accurate Timestamps

### Goal

Add accurate word-level timestamps without changing existing transcript-event
consumers.

### Work

- Add Qwen3-ForcedAligner-0.6B as an optional local model.
- Extend provider capabilities to report timestamp granularity.
- Run alignment only for finalized segments.
- Populate the existing optional `words` field.
- Preserve capture-derived segment timestamps as the fallback.
- Add subtitle export using aligned timestamps.
- Benchmark alignment latency and memory separately from transcription.
- Define behavior for unsupported languages and long segments.

### Acceptance criteria

- Supported English and Chinese segments receive monotonic word timestamps.
- Unsupported or failed alignment retains valid segment timestamps.
- Alignment cannot delay live caption finalization indefinitely.
- Existing Markdown output remains readable when word timestamps are disabled.

### Suggested commits

1. `feat: add optional forced-alignment provider`
2. `feat: populate word-level transcript timestamps`
3. `feat: export timestamped subtitle files`

## Milestone 9: Speaker Diarization

### Goal

Optionally group speech by speaker without coupling diarization to Qwen.

### Work

- Select a local diarization model compatible with the privacy and hardware
  requirements.
- Add a post-processing interface for finalized audio segments.
- Keep microphone/system source labels separate from speaker labels.
- Assign stable anonymous labels such as `Speaker 1` and `Speaker 2`.
- Define whether microphone speech receives a fixed local-speaker label.
- Allow users to rename speakers within a session.
- Store speaker labels in normalized events and Markdown.
- Add confidence and unknown-speaker behavior.

### Acceptance criteria

- Disabling diarization adds no model or latency overhead.
- Enabling it never removes the original source label.
- Speaker labels remain stable enough within one session to be useful.
- Diarization failures preserve the original transcript.

## Milestone 10: Optional Online Providers

### Goal

Allow users with limited local compute to select an online provider without
weakening local-only defaults.

### Work

- Extend provider discovery with capability and privacy metadata.
- Add an explicit consent flow before any audio may leave the Mac.
- Keep the service bound exclusively to loopback.
- Store credentials in Keychain, never transcript metadata or log files.
- Show a persistent Online indicator during external transcription.
- Add provider-specific file and streaming adapters behind the shared event
  contract.
- Define retry, rate-limit, quota, and billing-error behavior.
- Prevent silent fallback from local to online providers.
- Add a network kill switch that disables all online adapters.

### Acceptance criteria

- A fresh installation performs no online-provider requests.
- Local-only mode continues working with the network blocked.
- Online audio transmission requires an explicit provider selection and consent.
- Credentials are stored only in Keychain.
- Provider failures do not corrupt session files or cause an undisclosed
  fallback.

### Suggested commits

1. `feat: add provider capability and privacy metadata`
2. `feat: add online-provider consent and keychain storage`
3. `feat: add first optional online transcription adapter`
4. `test: prove local-only network isolation`

## Milestone 11: Windows and Linux Support

### Goal

Reuse the service and transcript protocol while replacing platform-specific
capture and UI components.

### Work

- Freeze and version the audio-stream and transcript-event protocol.
- Publish protocol conformance fixtures.
- Implement Windows microphone and WASAPI loopback adapters.
- Implement Linux microphone and PipeWire capture adapters.
- Add PulseAudio compatibility only where PipeWire is unavailable.
- Decide between platform-native shells and a shared cross-platform UI.
- Match source selection, permissions, captions, and session behavior as closely
  as each platform permits.
- Add CI build matrices for supported operating systems.

### Acceptance criteria

- Each platform adapter passes the same protocol conformance suite.
- Microphone and system audio remain separate sources on every platform.
- The Python service and Markdown persistence require no platform-specific
  branches beyond configuration.
- Unsupported capture features are shown explicitly rather than silently
  degraded.

## Cross-Cutting Quality Work

These tasks should accompany every milestone rather than wait for a final
hardening phase:

- Keep the API bound to loopback.
- Never download models or dependencies during normal application startup.
- Add schema versions before changing persisted metadata or wire events.
- Keep partial events out of Markdown.
- Preserve source identity through every transformation.
- Avoid unbounded queues and whole-session audio buffers.
- Add unit tests for pure logic and integration tests for process boundaries.
- Test English, Chinese, mixed-language speech, silence, noise, and overlap.
- Keep logs free of raw audio, transcript content by default, API keys, and other
  secrets.
- Update `README.md`, `PROJECT_SPEC.md`, and this roadmap when behavior changes.

## Recommended Next Sequence

The next development cycle should proceed in this order:

1. Build the signed macOS application shell.
2. Supervise and package the offline runtime.
3. Run long-session and crash-recovery tests.
4. Benchmark acceleration and incremental partial strategies in parallel.
5. Implement partial captions after selecting the inference strategy.
6. Add push-to-talk and the session browser.
7. Add forced alignment and diarization as optional modules.
8. Add online providers only after the local privacy boundary has automated
   regression tests.
9. Freeze the protocol before starting Windows and Linux adapters.

## Release Gates

### Internal alpha

- Signed macOS app.
- Offline installation works on a clean test Mac.
- Mic, all-system, selected-app, and dual-source capture work.
- One-hour real-session test passes.
- Known failures are surfaced in the UI.

### Public beta

- 24-hour soak test passes.
- Crash recovery and low-disk behavior are tested.
- Apple Silicon performance meets the selected latency target.
- Partial captions and push-to-talk are stable.
- Update and migration behavior is defined.
- Privacy behavior has automated network-isolation tests.

### Version 1.0

- Signed and notarized distribution.
- Documented hardware and macOS requirements.
- Reliable session browser, settings, and exports.
- Accessibility review for caption presentation and controls.
- Support and diagnostics workflow that does not expose transcript content.
- Forced alignment, diarization, and online providers may remain optional if
  their absence is clearly documented.
