# VoxInk Project Specification

Status: Working prototype implemented
Last updated: 2026-08-25

## 1. Product Summary

VoxInk is a privacy-first speech-to-text application that runs locally and can
transcribe microphone input, system audio, or both at the same time. It presents
near-live captions in a floating window and incrementally saves finalized
transcript segments to Markdown.

The first implementation targets macOS. The architecture must keep platform
audio capture, transcription providers, transcript processing, and user
interfaces separate so Windows and Linux support can be added later.

Local transcription is the default and must work with no network connection.
The transcription service will expose a localhost API. Optional online speech
providers may be added later for users who explicitly trade privacy for lower
local resource requirements.

## 2. Goals

- Run speech-to-text locally without network access.
- Capture microphone input, system audio, or both.
- Capture either all system audio or audio from one or more selected apps.
- Preserve the source of every transcript segment as `microphone` or `system`.
- Support English and Chinese, including automatic language detection.
- Show fast, replaceable partial captions and stable finalized captions.
- Write finalized transcription incrementally to Markdown.
- Support manually controlled, effectively unlimited recording sessions.
- Rotate transcript files so a long-running session never creates one enormous
  Markdown file.
- Make transcription backends replaceable behind a stable local API.
- Leave extension points for timestamps, speaker diarization, and online
  providers.
- Eventually support push-to-talk, continuous captions, and meeting
  transcription as presets over the same capture and transcription engine.

## 3. Non-Goals for the Initial Pipeline

- Production-quality speaker diarization or speaker identification.
- A polished final UI/UX.
- Cross-platform capture implementations.
- Direct typing into the currently focused application.
- Mandatory raw-audio recording.
- LAN or remote access to the transcription API.
- Guaranteed word-level timestamps from the first prototype.
- Online transcription-provider integrations.

## 4. Core User Experience

### 4.1 Session lifecycle

A session is one manually controlled capture run:

1. The user selects microphone, system audio, or both.
2. For system audio, the user selects all audio or one or more applications.
3. The user presses Start.
4. The user may Pause and Resume without creating a new session.
5. The user presses Stop to finalize the session.

A session has no fixed time limit. It may span multiple Markdown and audio files
through automatic file rotation.

### 4.2 Product presets

The following experiences share one underlying session engine:

- **Push-to-talk:** short microphone capture initiated by a manual control or
  global shortcut.
- **Live captions:** continuous transcription shown in a floating caption
  window and written to Markdown.
- **Meeting transcription:** microphone plus selected system sources, Markdown
  output, and optional audio retention.

Only the common pipeline needs to be proven initially. These presets can be
introduced incrementally.

### 4.3 Floating captions

The initial caption window should:

- Float above ordinary windows.
- Offer an optional click-through mode.
- Show approximately two to four recent caption blocks.
- Display microphone and system captions in one chronological stream.
- Mark every caption with a textual `MIC` or `SYSTEM` badge and distinct visual
  styling; color must not be the only source indicator.
- Temporarily stack overlapping microphone and system segments as separate rows
  within the same time region.
- Allow older content to move or fade out of the floating view while retaining
  the complete finalized history in Markdown.

UI details are provisional and will be refined after the pipeline works.

## 5. Audio Capture Requirements

### 5.1 Capture modes

- Microphone only.
- All system audio only.
- Selected application audio only.
- Microphone plus all system audio.
- Microphone plus one or more selected applications.

Microphone and system audio must remain independent streams. They must not be
mixed before transcription because source identity would be lost.

### 5.2 macOS implementation

- Use Apple's `AVAudioEngine` for microphone capture where appropriate.
- Use Apple's `ScreenCaptureKit` for system-audio capture and application
  filtering.
- Exclude VoxInk's own process audio by default to prevent feedback.
- Track selected applications by stable bundle identifier and automatically
  reconnect when an application exits and restarts.
- Handle microphone and screen/audio-capture permissions explicitly.

The first experimental microphone/file pipeline may be Python-only. Reliable
production system-audio capture should use a small native Swift component rather
than making the Python service directly own ScreenCaptureKit.

### 5.3 Future platform adapters

- Windows: WASAPI, including loopback capture for system audio.
- Linux: PipeWire where available, with PulseAudio compatibility if needed.

All platform adapters must emit the same normalized audio-frame representation.

### 5.4 Normalized audio

The capture layer should normalize audio before sending it to transcription:

- Mono PCM suitable for the active transcription provider.
- Explicit sample rate and sample format.
- Monotonic capture timestamp.
- Source identifier: `microphone` or `system`.
- Device or application metadata when available.
- Session identifier and stream identifier.

Exact sample rate and wire encoding will be selected after the Qwen pipeline is
benchmarked. A likely initial target is 16 kHz mono PCM.

## 6. Transcription Behavior

### 6.1 Partial and final results

- Partial results should appear quickly and may be replaced as more context
  arrives.
- Final results are stable and are the only results appended to Markdown.
- The initial finalization policy should use voice activity/silence detection,
  provisionally finalizing after roughly 600-800 ms of silence.
- Enforce a maximum audio chunk duration and retain a small overlap between
  adjacent chunks to reduce clipped words.
- Deduplicate text introduced by overlapping chunks before finalization.

The exact thresholds must remain configurable and should be tuned using real
English and Chinese speech.

### 6.2 Languages and punctuation

- Initial languages: English and Chinese.
- Default language mode: unresolved; proposed default is automatic detection
  with manual English and Chinese overrides.
- Punctuation is expected from the speech-to-text model.
- Partial punctuation may change as later speech provides more context.

### 6.3 Timestamps

- Every segment must have capture-derived start and end timestamps.
- Accurate forced alignment is a later enhancement.
- Qwen3-ForcedAligner-0.6B is the proposed Qwen timestamp component but is not
  currently part of the workspace.
- The data model must allow future word- or token-level timestamps without an
  incompatible API change.

### 6.4 Speaker information

- Speaker diarization/identification is not required for the first pipeline.
- Transcript events include an optional speaker field from the start.
- A future diarization module may annotate finalized segments after
  transcription.

## 7. Local Transcription Service

### 7.1 Process boundary

The Python transcription service owns:

- Model discovery and lifecycle.
- Local Qwen inference.
- Audio buffering, chunking, and voice activity detection.
- Partial/final transcript generation.
- Transcript normalization and overlap deduplication.
- Markdown persistence and file rotation.
- Optional audio persistence and rotation.
- Provider selection and privacy enforcement.

The capture/UI process should not contain Qwen-specific logic.

### 7.2 Network and privacy policy

- Bind only to `127.0.0.1`; never bind to all interfaces.
- Do not expose the API to the LAN.
- Local mode must make no outbound network requests.
- Online providers must be disabled unless the user explicitly enables and
  selects one.
- The UI should make the active provider and privacy mode visible.
- Provider credentials must never be written into transcript metadata.

### 7.3 Proposed API surface

The precise routes may change during implementation, but the service needs:

- A health/readiness endpoint.
- A model/provider discovery endpoint.
- A session creation and configuration endpoint.
- Start, pause, resume, and stop session controls.
- A batch/file transcription endpoint.
- A streaming audio connection, likely WebSocket.
- A transcript-event stream, likely on the same WebSocket.

The external protocol must use provider-neutral request and event types. Batch
routes may follow familiar OpenAI-style conventions where helpful, but live
streaming needs a VoxInk-specific event contract.

### 7.4 Provider interface

The backend must define a common provider contract, conceptually:

```python
class TranscriptionProvider:
    async def load(self) -> None: ...
    async def transcribe_file(self, audio, options): ...
    async def transcribe_stream(self, frames, options): ...
    async def unload(self) -> None: ...
```

Initial provider:

- `QwenLocalProvider` using the local `Qwen3-ASR-1.7B` model.

Potential future providers:

- Other local models.
- Explicitly enabled online speech-to-text services.

Online-provider adapters must not weaken the guarantees of local-only mode.

## 8. Transcript Event Model

All providers should produce a normalized event similar to:

```json
{
  "session_id": "2026-08-25_14-30-00",
  "segment_id": "seg-000042",
  "stream_id": "system-1",
  "source": "system",
  "start_ms": 12450,
  "end_ms": 15820,
  "language": "en",
  "text": "Let's review the proposal.",
  "is_final": true,
  "speaker": null,
  "words": null
}
```

Required properties:

- Session, stream, and segment identity.
- Source identity.
- Start and end time.
- Text.
- Partial/final state.

Optional or extensible properties:

- Detected language and confidence.
- Speaker identity.
- Word/token timestamps.
- Provider-specific diagnostics kept outside the readable transcript.

## 9. Markdown Output

### 9.1 Directory structure

One logical session gets one directory:

```text
2026-08-25_14-30-00/
├── transcript-part-001.md
├── transcript-part-002.md
├── microphone-part-001.wav    # only when enabled
├── system-part-001.wav        # only when enabled
└── combined-part-001.wav      # only when enabled
```

File names and audio formats are provisional.

### 9.2 Rotation

Rotate a Markdown file when either limit is reached:

- Six hours of session time in the current part.
- 5 MiB file size.

Rotation must not interrupt capture or reset session-relative timestamps. Part
numbers increase monotonically. The limits should become configurable later.

Audio files require their own safe rotation rules because uncompressed audio can
grow much faster than Markdown and legacy WAV has size limitations.

### 9.3 Durability

- Append only finalized segments.
- Flush writes frequently.
- Use atomic creation/rotation where practical.
- Recover an interrupted session on the next launch without overwriting data.
- Mark whether a part ended normally or was recovered after interruption.

### 9.4 Metadata

Each Markdown part contains YAML front matter. Proposed fields:

```yaml
---
schema_version: 1
session_id: "2026-08-25_14-30-00"
part: 2
session_started: "2026-08-25T14:30:00-04:00"
part_started: "2026-08-25T20:30:00-04:00"
sources: [microphone, system]
applications: ["Zoom", "Safari"]
model: "Qwen3-ASR-1.7B"
provider: "qwen-local"
languages: ["en", "zh"]
audio_saved: false
---
```

Do not store secrets or provider credentials in metadata.

### 9.5 Transcript format

Proposed readable format:

```markdown
**[00:00:03.200-00:00:06.100] [SYSTEM]**
Let's review the proposal.

**[00:00:05.450-00:00:08.000] [MIC]**
Yes, I have it open now.
```

Overlapping speech remains as separate, source-labeled segments ordered by start
time.

## 10. Optional Audio Retention

- Audio recording is off by default.
- Users may save separate microphone and system tracks.
- Separate tracks are the default when saving is enabled.
- Users may alternatively or additionally save a combined track.
- Audio-save settings are captured in session metadata.
- Rotation and crash recovery must be designed before long-running recording is
  considered reliable.

The initial prototype may use WAV for simplicity. A compressed, lossless format
such as FLAC should be evaluated for long-running sessions.

## 11. Component Layout

```text
VoxInk
├── clients/
│   ├── macos_capture/          # Native capture library, CLI, and caption panel
│   └── python_capture/         # Experimental Python microphone client
├── services/
│   └── transcription/          # Local HTTP/WebSocket transcription service
├── voxink_core/
│   ├── providers/              # Qwen and future provider adapters
│   └── persistence/            # Markdown/audio writers and rotation
├── tests/
│   ├── unit/                   # Core and capture-client unit tests
│   └── integration/            # Service API and boundary tests
└── models/
    └── Qwen3-ASR-1.7B/         # Ignored local model files
```

The source tree follows this layout. Runtime model weights and local virtual
environments remain outside version control.

## 12. Implementation Plan

### Phase 0: Prove local batch inference

- Create an isolated Python environment and pin dependencies.
- Load the local Qwen3-ASR-1.7B model without downloading anything at runtime.
- Transcribe a local WAV fixture through a provider abstraction.
- Normalize the result into the transcript-event schema.
- Write finalized output to a Markdown part with metadata.
- Add a health endpoint and batch transcription endpoint.
- Verify that the complete flow works with network access disabled.

Exit criterion: a local audio file becomes a source-labeled Markdown transcript
through the localhost API.

### Phase 1: Live microphone pipeline

- Capture live microphone PCM.
- Add resampling and channel normalization.
- Add voice activity detection and chunk formation.
- Produce replaceable partial results and finalized results.
- Exercise pause/resume/stop and crash-safe Markdown output.
- Measure transcription latency and memory usage.

Exit criterion: an unlimited manually controlled microphone session produces
near-live captions/events and durable Markdown.

### Phase 2: Native macOS system capture

- Build a minimal Swift ScreenCaptureKit capture component.
- Enumerate all-system and selected-app capture choices.
- Support multiple selected applications.
- Emit normalized system-audio frames to the local service.
- Capture mic and system as distinct simultaneous streams.
- Exclude VoxInk audio.
- Reconnect restarted applications by bundle identifier.
- Handle permissions and error states.

Exit criterion: mic and selected system sources can be transcribed together with
correct source labels.

### Phase 3: Floating caption experience

- Add the always-on-top caption window.
- Add source badges, partial/final styling, and overlap layout.
- Add optional click-through behavior.
- Add capture controls and visible privacy/provider state.
- Add transcript destination and audio-retention settings.

Exit criterion: the end-to-end macOS experience is usable without a terminal.

### Phase 4: Accuracy and resilience

- Tune VAD, chunk duration, overlap, and deduplication for English and Chinese.
- Evaluate Qwen forced alignment for improved timestamps.
- Add session recovery, file rotation tests, and long-duration soak tests.
- Add optional audio compression.
- Design a diarization plug-in point without enabling it by default.

### Phase 5: Extensibility

- Add push-to-talk and meeting-mode presets.
- Add provider configuration and explicit online-provider consent flow.
- Evaluate cross-platform UI strategy.
- Implement Windows and Linux capture adapters.

## 13. Testing Strategy

- Unit tests for event schemas, time calculations, overlap deduplication, and
  file rotation.
- Golden Markdown tests for formatting and metadata.
- Audio fixtures covering English, Chinese, silence, noise, and overlapping
  microphone/system speech.
- Integration tests for file-to-API-to-Markdown flow.
- Tests proving local-only mode attempts no network access.
- Permission denial and capture-source disappearance tests.
- Application restart/reconnect tests.
- Crash recovery and multi-part session tests.
- Long-duration soak tests before declaring sessions unlimited in production.

## 14. Current Model State

- The workspace contains `models/Qwen3-ASR-1.7B`.
- The two model weight shards currently occupy approximately 4.4 GB.
- The model has loaded successfully on CPU and transcribed the English sample
  through both the direct script and the localhost HTTP API.
- The installed Torch build currently reports MPS unavailable, so Apple GPU
  acceleration remains an optimization task.
- Model choice and runtime are replaceable implementation details, not permanent
  product constraints.

## 15. Remaining Work

The prototype resolves the initial platform and protocol decisions: macOS 13+,
native Swift/AppKit/SwiftUI capture and captions, a loopback-only versioned
HTTP/WebSocket API, automatic English/Chinese detection with overrides,
`~/Documents/VoxInk/` output, 700 ms energy-VAD finalization, and rotating WAV
retention.

The following remain after the initial implementation:

1. Package the native components as a signed menu-bar `.app` with reliable
   privacy permission identity.
2. Produce true incremental model partials; the current live path emits stable
   results after VAD finalization.
3. Benchmark and enable an accelerated Apple Silicon inference runtime.
4. Add push-to-talk clipboard behavior and global-shortcut configuration.
5. Add Qwen forced alignment for word-level timestamps.
6. Add optional speaker diarization.
7. Add explicit-consent online-provider adapters.
8. Add Windows and Linux capture implementations.
9. Run long-duration capture and file-recovery soak tests on real meetings.

## 16. Design Principles

- Local-only means no network activity, not merely no cloud transcription.
- Capture sources remain separate until after transcription.
- Partial text is disposable; finalized text is durable.
- Long-running sessions are logical containers, not single files.
- Provider-specific behavior stays behind a stable internal interface.
- Platform-specific capture stays behind a normalized audio interface.
- Optional features must not weaken the default privacy guarantees.
- Build and measure the smallest pipeline before optimizing the model or UI.
