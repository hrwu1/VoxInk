# VoxInk Project Specification

Status: Canonical architecture, roadmap, and shipping reference

This file is the single source of truth for VoxInk's product architecture,
implemented extensions, remaining work, and production-release requirements.
Sections 1-15 define the product and current technical contracts, section 16
tracks verified state and open work, and section 17 defines what "shipped"
means. Update open-item status in section 16 as evidence changes; keep durable
requirements in their owning technical or shipping section.

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

#### Menu-bar recording shortcut

Control + Shift + R starts a normal recording when idle and stops/finalizes
it when recording or paused. It uses the source selected in the menu bar
(microphone by default), together with the saved recording options.
The menu has one Start Recording / Stop Recording control with the shortcut
beside it, plus an Audio Source submenu for Microphone, System audio, or both.
Selecting a source saves it immediately without starting capture; a checkmark
and the submenu title show the selection. Source selection is disabled during
a session, pending commands, and push-to-talk.
Push-to-talk keeps Control + Option + Space; the recording shortcut is inactive
while push-to-talk is enabled. Held keys and presses during a pending command
must not queue additional actions. Registration failures must be visible in the
menu, and neither shortcut launches the app or helper when they are closed.

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

One logical session gets one directory. Automatically generated session IDs use
local wall-clock time in the stable, filesystem-safe format
`YYYY-MM-DD_HH-MM-SS-ffffff`:

```text
2026-08-25_14-30-00-123456/
├── transcript-part-001.md
├── transcript-part-002.md
├── microphone-part-001.wav    # only when enabled
├── system-part-001.wav        # only when enabled
└── combined-part-001.wav      # only when enabled
```

The timestamp uses six fractional-second digits. Every capture entry point must
generate the same form when the caller does not supply a session ID. Explicit,
path-safe session IDs remain valid. Existing session directories are never
renamed. ISO 8601 remains appropriate for timestamps stored inside metadata and
diagnostics; this rule applies to the session ID and directory name only.

Transcript and recording part names are stable. Part numbers are zero-padded to
three digits, start at `001`, increase monotonically, and never overwrite an
existing part. Matching microphone, system, and combined audio part numbers are
time-aligned. Transcript parts rotate independently and must not be assumed to
cover the same interval as an audio part with the same number. Audio formats
remain provisional.

Standardization goal and scope:

- Keep new session directories predictable across the service, developer
  launcher, standalone Python capture, and native Swift capture.
- Keep one shared Python generator in `voxink_core`; the Swift client mirrors
  the same cross-language contract.
- Preserve caller-provided session IDs and all existing artifact directories.

Completion requires deterministic formatter tests in Python and Swift, a scan
showing no capture entry point retains a legacy timestamp formatter, and the
applicable Python and Swift test suites passing.

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

- Audio recording is on by default in the developer workflows, with a visible
  pre-session control and a launcher opt-out.
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
│   ├── macos_capture/          # Native capture backend and caption panel
│   └── python_capture/         # Portable microphone backend (no Swift build)
├── services/
│   └── transcription/          # Local HTTP/WebSocket transcription service
├── voxink_cli/                 # Public `voxink` command and shared control client
├── voxink_mcp/                 # MCP server over the same control client
├── voxink_menubar/             # Native menu-bar control surface (Swift package)
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

## 12. Testing Strategy

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

Current implementation status and priorities are maintained in section 16;
setup and test commands are maintained in `README.md`.

## 13. Design Principles

- Local-only means no network activity, not merely no cloud transcription.
- Capture sources remain separate until after transcription.
- Partial text is disposable; finalized text is durable.
- Long-running sessions are logical containers, not single files.
- Provider-specific behavior stays behind a stable internal interface.
- Platform-specific capture stays behind a normalized audio interface.
- Optional features must not weaken the default privacy guarantees.
- Build and measure the smallest pipeline before optimizing the model or UI.

## 14. Public CLI and Agent Interface

### 14.1 Goal and scope

VoxInk should provide a public `voxink` command that lets people, scripts, and
AI agents use the same supported capabilities as the native application without
screen scraping or parsing human-oriented terminal output. The CLI is a client
of VoxInk's local control plane, not a second implementation of capture,
transcription, persistence, model management, or recovery.

The public interface must preserve the existing product boundaries:

- Local operation remains offline-enforced and source-aware.
- Finalized transcripts and retained audio remain user-owned artifacts.
- Capture, model, and persistence behavior remains provider-neutral at the
  public boundary.
- The signed application or its bundled background service owns helper
  lifecycle, credentials, permissions, and recovery.
- Agents cannot obtain sensitive permissions merely by invoking the CLI.

The existing `scripts/voxink-dev`, Python capture client, and Swift capture CLI
remain development and diagnostic tools. They are not the public contract.

### 14.2 Shared control architecture

The native application, public CLI, and future agent adapters must call one
shared domain service:

```text
macOS application ---+
public voxink CLI ----+--> local control API --> capture adapters
agent adapter/MCP ----+          |              transcription providers
                                 |              session registry
                                 +------------> transcript/audio artifacts
```

The domain service owns validated operations for capability discovery, capture
control, session lookup, event streaming, file transcription, transcript and
audio discovery, model selection, vocabulary management, configuration, and
sanitized diagnostics. UI-specific routes may adapt these operations but must
not contain the only implementation of a product capability.

Production IPC should use a Unix-domain socket, XPC, or an equivalently private
local mechanism. Credentials must not appear in command-line arguments,
transcripts, persistent logs, or agent-visible configuration. Development HTTP
and WebSocket adapters may remain available behind the same domain interfaces.

### 14.3 Public command groups

The public command hierarchy is organized by product resource rather than by
the current process layout:

| Command group | Required operations |
| --- | --- |
| `voxink capabilities` | Report supported, available, and permitted features. |
| `voxink status` and `voxink doctor` | Report readiness and actionable local failures. |
| `voxink devices` | List usable microphone devices. |
| `voxink applications` | List applications available for selected-app capture. |
| `voxink capture` | Start, inspect, watch, pause, resume, and stop live capture. |
| `voxink sessions` | List and inspect active, completed, and interrupted sessions and their artifacts. |
| `voxink transcribe` | Transcribe an existing local audio file. |
| `voxink transcripts` | Read, follow, search, export, and locate transcript artifacts. |
| `voxink audio` | List, locate, and export retained source or combined audio. |
| `voxink models` | List, inspect, verify, select, and explicitly import local models. |
| `voxink vocabulary` | Read, validate, add, remove, replace, import, and export terms and corrections. |
| `voxink config` | Read schemas and validated settings and update permitted settings. |
| `voxink diagnostics` | Read sanitized state and produce a privacy-reviewed diagnostic bundle. |
| `voxink permissions` | Inspect pending requests and existing agent grants and revoke grants. |

The first implementation slice includes capability/readiness discovery,
capture input discovery, the complete capture lifecycle, session and artifact
lookup, file transcription, transcript retrieval, model listing/selection, and
vocabulary reading/replacement. Search, export conveniences, diagnostic bundles,
and derived agent artifacts follow only after the first slice works end to end.

### 14.4 Machine-readable contract

Every public command must support explicit `text`, `json`, and where streaming
is appropriate `jsonl` formats. Machine-readable output uses a versioned,
provider-neutral envelope:

```json
{
  "schema_version": "1",
  "ok": true,
  "command": "capture.start",
  "data": {},
  "warnings": []
}
```

Failures use stable error codes rather than requiring consumers to parse prose:

```json
{
  "schema_version": "1",
  "ok": false,
  "command": "capture.start",
  "error": {
    "code": "approval_required",
    "message": "System-audio capture requires authorization.",
    "retryable": true,
    "request_id": "req_123",
    "details": {}
  }
}
```

The initial error taxonomy includes `invalid_request`, `not_ready`,
`not_found`, `permission_denied`, `approval_required`, `invalid_transition`,
`conflict`, `capture_unavailable`, `model_busy`, `disk_low`, `timeout`, and
`internal_error`. The CLI writes structured results to stdout, diagnostics to
stderr, and returns documented stable exit codes.

Commands support `--format`, `--schema-version`, `--timeout`, `--request-id`,
and `--no-input`. Mutating operations support an idempotency key. Operations
that expose a proposed state change support `--dry-run` where meaningful, and
long-running transitions support `--wait`. Event streams use monotonically
increasing sequence numbers and resumable cursors so an agent can reconnect
without duplicating or silently missing events.

Every successful mutation returns its effective configuration, resource ID,
state, and revision. State-changing requests may carry an expected revision to
prevent one controller from unknowingly overwriting another controller's
decision.

### 14.5 Authorization and auditability

Read-only capability and readiness discovery may be available automatically.
Sensitive operations require grants issued by the native application, never by
the CLI itself. Initial scopes are:

- `capture.microphone`
- `capture.system`
- `capture.applications`
- `sessions.read`
- `transcripts.read`
- `audio.read`
- `audio.retain`
- `vocabulary.write`
- `models.switch`
- `models.import`
- `artifacts.delete`

Grants may be limited by application bundle identifier, capture source,
recording duration, output location, retention behavior, expiration time, or
single use. Starting unattended capture is allowed only when the user has
created a matching scoped grant in the application. Missing authorization
returns `approval_required` with a request identifier that the application can
present to the user. A command-line confirmation flag cannot bypass this gate.

Control mutations record an audit entry containing the time, operation,
request ID, client identity, affected session, result, and permission scope.
Audit entries must not contain transcript text, raw audio, credentials, or
unnecessary command arguments.

### 14.6 Durable resources

The agent interface depends on a versioned session registry rather than
process-local session objects or Markdown scanning. Each session record includes
its lifecycle state, revision, enabled sources, selected applications,
languages, provider/model versions, retention policy, timestamps, completion or
recovery status, and artifact inventory. It contains paths and metadata but no
credential or transcript text.

Transcript events retain stable event IDs, sequence numbers, capture-derived
timestamps, source labels, final/partial state, and optional speaker fields.
Partial events remain disposable; finalized events remain durable. Session
artifacts identify every Markdown part and microphone, system, or combined audio
part without requiring filename conventions to be reverse engineered.

Agents may later attach summaries, action items, translations, chapters,
speaker labels, or proposed corrections as derived artifacts. Derived artifacts
must not silently rewrite the canonical transcript and must record their
producer, source session, source transcript revision, creation time, and human
approval state when applicable.

### 14.7 Compatibility and evolution

CLI flags and JSON fields are a versioned public API. Additive fields are
permitted within a schema version; removals, semantic changes, and renamed
error codes require a new schema version. Capability discovery reports both
protocol and output-schema versions so clients can reject incompatible
combinations before starting capture.

The CLI and future MCP or SDK adapters share the typed domain client and schema
definitions. An agent adapter must not shell out to the CLI and parse terminal
text when it can call the shared client directly.

### 14.8 Design completion criteria

This interface design is ready for implementation planning when:

1. The public CLI is confirmed as a thin client of the application-owned local
   control service.
2. The first command slice and deferred command groups are accepted.
3. JSON envelopes, errors, event cursors, idempotency, and revision behavior are
   accepted as public compatibility commitments.
4. Unattended capture is confirmed to require an application-issued scoped
   grant.
5. Durable session and artifact manifests are accepted as prerequisites rather
   than CLI-only filesystem scanning.
6. Canonical transcripts remain immutable to agents; agent output is stored as
   provenance-labelled derived artifacts.

## 15. Implemented Extensions (2026-09-02)

Additive notes for behaviour the sections above left as extension points.
Each keeps the guarantees of sections 7.2 and 13.

### 15.1 System audio backends and channels

`clients/macos_capture` selects a system-audio backend: Core Audio process
taps (macOS 14.2+, the default) or ScreenCaptureKit (fallback). With taps,
selected-application capture opens one stream per application. Such a stream
carries `channel` (bundle identifier) and `channel_name` in `stream.start`,
its transcript events carry the same fields, the Markdown source tag becomes
`[SYSTEM: Name]`, and retention writes `system-<bundle>-part-NNN.wav`.

### 15.2 Partial captions

While an utterance is open the service may emit `transcript.partial` events
with `is_final: false` under one segment id; the final for that utterance uses
the same id. Partials are never persisted. `/health` reports
`capabilities.partial_results`.

### 15.3 Echo suppression

When a session has both microphone and system streams, a microphone chunk
whose normalized cross-correlation with a system channel's recent audio (lags
up to 300 ms) exceeds a threshold is dropped and reported as
`audio.echo_suppressed`. The system audio is never transcribed twice.

### 15.4 Annotations

Two non-transcript blocks may appear in canonical parts: a marker,
`**[HH:MM:SS.mmm] [MARK]**` followed by an optional label line, and a quoted
translation, `> [HH:MM:SS.mmm · lang] text`. Readers of canonical parts
(`voxink_core.diarization.transcript`) skip both.

### 15.5 Translation

`TranslationProvider` (`voxink_core.translation`) translates finalized text on
device. The service publishes `transcript.translation` events and a
`translation` feed entry; the canonical block is unchanged.

### 15.6 Grants and audit (section 14.5, implemented)

`voxink_core.grants.GrantStore` holds grants (scopes, application allow-list,
`max_duration_s`, `retain_audio`, expiry, single use) and pending requests
with claim secrets. Grant tokens (`vxg_…`) authenticate the control API; the
owner credential remains unrestricted. Missing scope returns HTTP 403 with
`{"code": "approval_required", "request_id": …, "claim_secret": …}`. A caller
holding no credential is refused earlier, while being authenticated, so it
cannot reach that answer from the service; `voxink_mcp` files the request for
it from the scopes its tool needs and returns the same shape, which is what
section 14.5 requires of missing authorization. `GET /v1/grants/self` reports
the caller's own credential and effective scopes, as distinct from
`/v1/grants/scopes`, which is only the catalog a grant may carry. Every
control mutation is appended to `audit.jsonl`. `voxink_mcp` is the agent
adapter over the shared client.

### 15.7 Speaker identity

`DiarizationResult.embeddings` carries one voice embedding per anonymous
speaker; `speaker-turns.json` stores them under `speakers`, with optional
`name`, `registry_id`, and `similarity`. `voxink_core.speakers.SpeakerRegistry`
(state directory, 0600, opt-in) matches later sessions by cosine similarity.
Names are rendered in the derived Markdown view only.

## 16. Current Status and Remaining Work

Status: Active roadmap. Last verified against the tree on 2026-09-02.

This section tracks work remaining after the developer workflow, native capture
integration, and the 2026-09-02 feature push. Completed implementation plans
are removed once their durable requirements are reflected in this specification,
`README.md`, or tests; Git history retains their implementation detail.
`.claude/plans/feature-push.md` records the 2026-09-02 push.

Priority follows observed evidence: defects affecting current sessions first,
then advertised but incomplete capabilities, verification of frequently changed
code, and finally convenience. Work without an observed problem belongs under
Deferred rather than in a priority tier.

### 16.1 Verified state

Confirmed by reading the tree and running the checks on 2026-09-02:

- A one-command workflow through `./scripts/voxink-dev`, which starts the
  menu-bar application by default, the browser workflow through `--web`, and a
  public `./scripts/voxink` command.
- System audio through Core Audio process taps, with ScreenCaptureKit as the
  fallback. A target-Mac muted-player run produced 837 frames, 99% nonzero
  bytes, and a finalized transcript. The same run through ScreenCaptureKit
  could not create a display-backed filter.
- Partial captions while an utterance is open, replaced in place by the final
  and never written to Markdown.
- One labelled channel per selected application, one WAV track per channel,
  and suppression of microphone echo from a system channel.
- Timestamped markers, microphone selection, language override, Finder reveal,
  and browser settings.
- On-device translation of finalized captions with a local Qwen3 instruct
  model, shown as a second line and quoted in Markdown.
- Speaker diarization after Stop with forced-alignment splitting, per-speaker
  embeddings, and an opt-in local registry for remembered voices.
- Scoped agent grants, application allow-lists, duration and retention limits,
  expiry, single-use approval and revocation flows, audit records, and an MCP
  server over the shared client.
- Bounded audio retention, interrupted-part recovery, and an adaptive speech
  gate.

Recorded checks on 2026-09-02:

- `.venv/bin/python -m unittest discover -s tests`: 603 passing tests.
- `swift test` in `clients/macos_capture`: 15 passing tests.
- `npm test` in `frontend`: 6 passing tests; `npm run build:local` succeeded.

These counts are historical evidence, not a substitute for rerunning the suites
after later changes.

#### Recording shortcut implementation (2026-09-09)

Goal: add the normal-recording shortcut described in §4.2.

- [x] Inspect recording control, push-to-talk, and existing global registration.
- [x] Add source persistence, guarded start/stop dispatch, and menu feedback.
- [x] Test source restoration, repeat/transition guards, and hotkey routing.
- [x] Build the native menu-bar app with `scripts/build-macos-menubar`.
- [ ] Verify the native menu and global shortcut interactively (see limitation
  below); distinguish UI/control verification from actual audio capture.
- [x] Review, commit, and fast-forward into `main` (`032cda59`); rebuild the
  main development app and remove the temporary worktree.

Completion requires passing relevant Swift tests/build, verified UI/control
behavior or an explicit record of what the environment prevented, and updated
user documentation. Existing audio capture and transcription are reused.

Validation: `swift test --package-path clients/macos_capture` passed 52 tests,
including eight new menu-bar shortcut tests. The development app build passed.
An isolated app was launched against a simulated helper on port 8398 without
capturing audio. Computer Use timed out attaching to the app by both path and
bundle ID. A shortcut sent through Finder produced no simulated helper commands;
the test process was alive in its normal AppKit event loop. Consequently native
key delivery, menu presentation, and recording through the new shortcut remain
unverified; this attempt is not an end-to-end pass. The existing VoxInk process
was left running. Restart the rebuilt menu-bar app to load the change.

Follow-up: the user confirmed recording worked, then identified a conflict.
Rectangle's Center Two Thirds also uses Control + Option + R. The user selected
Control + Shift + R as the replacement. Keep the recording behavior and
Control + Option + Space push-to-talk binding unchanged.

- [x] Change the recording registration and visible menu/documentation hints.
- [x] Run Swift tests and rebuild the development app; report native UI results.
- [x] Commit this binding change. Completion requires matching registration,
  menu hints, and documentation, with passing tests/build.

Replacement validation: all 52 Swift tests passed and the development app
rebuilt successfully. Computer Use sent Control + Shift + R from Finder to an
isolated app connected to a simulated helper, but no control request arrived.
Native delivery of the replacement remains unverified by automation. The test
app was stopped; the user's existing instance was preserved and needs a restart
to load the rebuilt shortcut.

#### Separate recording control and source (2026-09-09)

Goal: separate Start/Stop from source selection using the existing saved source.

- [x] Replace source-specific Start actions with one Start/Stop control and a
  checked Audio Source submenu; persist selection without capture.
- [x] Run Swift tests and rebuild the development app.
- [x] Attempt native menu interaction and record environment limitations below.
- [x] Review and commit the focused change.

Completion requires independent source selection, the same source for menu and
shortcut starts, disabled source changes during a session, and passing build
and relevant tests. Audio capture and push-to-talk retain their existing paths.

Validation: all 52 Swift tests passed, including saved-source restoration and
recording state guards. `scripts/build-macos-menubar` and `git diff --check`
passed. An isolated app launched against a simulated helper on port 8398;
Computer Use timed out attaching to its bundle, and Finder's screenshot exposed
only its window, not the status bar. Native menu presentation, source clicks,
and Start/Stop interaction therefore remain unverified. No simulated capture
commands were issued. The isolated app/helper were stopped; restart the rebuilt
development app to load this change in the user's running instance.

### 16.2 P0: Target-Mac acceptance of capture paths

The tap backend delivered real all-system audio from a muted player. The
following modes still require a person at the target Mac:

- [ ] Exercise per-application taps with a real application such as Zoom or
  Safari. Verify one channel per application, reconnection after application
  restart, and the `[SYSTEM: Name]` transcript label.
- [ ] Exercise microphone plus system capture with speakers on. Observe echo
  suppression against real room echo and tune `EchoPolicy.min_correlation` if
  it fires on the user's own speech.
- [ ] Exercise floating captions and push-to-talk from the menu-bar application,
  including the Accessibility prompt for pasting.
- [ ] Exercise partial captions with the 1.7B model on MPS and confirm the
  interval stretches rather than queueing ahead of final inference.

Complete this priority when each mode has been exercised once on the target Mac
and every discovered defect has a regression test.

### 16.3 P1: Speaker recognition quality

Registry, matching, and naming are covered end to end with synthetic embeddings;
real recognition is not. In the available far-field session, separate clips
from the dominant speaker scored cosine 0.27, below the 0.62 threshold.

- [ ] Record two clean sessions of the same person and set the fallback
  embedding threshold from measured similarity rather than literature.
- [ ] Compare the pipeline centroid with the per-speaker fallback on a
  multi-speaker session. Observed agreement was 0.86 for one speaker and 0.14
  for a speaker with short turns.
- [ ] Decide whether system-channel speakers use their own registry namespace
  or remain session-local.

### 16.4 P1: Translation quality

The 0.6B model translates in about 0.2 seconds per sentence and follows the
prompt, but observed output dropped clauses and mistranslated jargon such as
"churn."

- [ ] Compare Qwen3-1.7B on the same sentences; the target Mac can hold it
  alongside the 1.7B ASR model.
- [ ] Carry vocabulary terms into the translation prompt so names and product
  terms survive.

### 16.5 P1: Test frequently changed boundaries

- [ ] Test the recorder hook's capture-lifecycle ownership; only its caption
  feed merge has been extracted and tested.
- [ ] Add a focused failure test for the helper dying mid-session.
- [ ] Test malformed traffic and model corruption at the service boundary.

### 16.6 P1: Durable session discovery and recovery

As last verified, `sessions list` reports only sessions held open by the running
helper, while `last_session` in the capture view is the only pointer to the most
recently finished session. Promote this work when session volume or agent use
makes directory listing insufficient.

### 16.7 P2: Production macOS application

The development menu-bar app drives the helper through the control API. The
remaining production work is defined in section 17: application-owned helper
lifecycle and permissions, self-contained runtime and model setup, signing,
notarization, installation, and updates.

### 16.8 Deferred

- Long-run and deterministic replay infrastructure.
- The remaining public CLI surface: `audio`, `config`, and `diagnostics` command
  groups; transcript search and export; and derived agent artifacts.
- Owner-credential isolation. A process running as the user can currently read
  `control-endpoint.json`. Grants are enforced for grant tokens, but keeping the
  owner credential out of reach requires signed-application work.
- A testable offline-network assertion.

### 16.9 Developer-edition gates

1. One command reaches ready state without manual port or helper management.
2. All advertised capture modes pass target-Mac acceptance.
3. Capture preserves quiet speech across microphones and input gains, while
   silence produces no text.
4. Local IPC remains authenticated, versioned, and loopback-only.
5. Finalized transcripts survive interruption and recovery without overwrite.
6. Slow inference, long sessions, and a full disk keep memory and storage
   bounded and never cost finalized text.
7. Offline tests detect no unexpected network access.
8. Model, permission, device, helper, and disk failures provide useful recovery
   actions.
9. Python, Swift, frontend, real-model smoke, replay, and soak checks pass in the
   supported development environment.

As of 2026-09-02, gates 1, 4, and 5 were met. Gate 9 was met for the Python,
Swift, and frontend suites. Gate 2 was met for all-system capture and remained
open for per-application and combined modes. Gate 3 was met in code and fixtures.
Gates 6, 7, and 8 remained open.

## 17. Production Shipping Specification

Status: Proposed.

### 17.1 Shipping goal and initial scope

Ship VoxInk as a signed, notarized macOS menu-bar application that can be
installed and used without a terminal or developer environment. On a clean
supported Mac, a user must be able to complete permission and model setup,
choose microphone and system-audio sources, start a session, see responsive
captions, stop, and open the saved transcript from the application.

The first production release targets Apple Silicon Macs running macOS 13 or
newer and supports:

- Local-only speech recognition.
- Microphone-only, all-system, selected-application, and combined microphone
  plus system capture with separate source labels.
- English and Chinese transcription with automatic detection or an explicit
  language preference.
- Floating captions and Markdown transcript persistence.
- Optional separate and combined audio retention.
- Manually controlled start, pause, resume, and stop.

Production-quality speaker identity, cloud providers, Windows and Linux,
guaranteed word-level timestamps, direct typing into other applications, and a
mobile application are not version-one release blockers.

### 17.2 Installation and first launch

The user installs and launches one signed application. VoxInk must not require
Homebrew, Python, Xcode, command-line tools, or manual shell commands.

First-run setup must:

1. Explain that transcription runs locally.
2. Request microphone permission when microphone capture is selected.
3. Request Screen & System Audio Recording permission when system capture is
   selected.
4. Explain when macOS requires an application restart after a permission change.
5. Locate, validate, import, or install the transcription model and runtime.
6. Show required disk space and installation progress.
7. Confirm readiness or present a recoverable error.

Users may import a verified offline model package. Any offered download must be
explicit and must not weaken offline operation after setup.

### 17.3 Product experience

The menu-bar interface must provide:

- State: not ready, ready, starting, recording, paused, stopping, or failed.
- Microphone selection and system selection of none, all audio, or one or more
  running applications.
- Start, Pause, Resume, and Stop controls; a visible recording indicator; and
  elapsed time.
- Caption visibility and click-through controls.
- Transcript destination, audio-retention controls, and storage implications.
- Language selection with an automatic option.
- Open Current Transcript and Open Transcript Folder actions.
- Settings, diagnostics, help, and Quit.

The UI must prevent invalid combinations before capture begins.

The floating caption window must stay above ordinary windows, support optional
click-through, show microphone and system captions chronologically, identify
sources with text as well as color, replace partials without duplication,
preserve finals in the transcript, remain legible across appearances and common
display scales, and remember its position and appearance.

Errors must use user language and offer a useful next action. This includes
permission denial, missing models, low disk space, unavailable devices, selected
applications not running, helper failure, and transcription lag. Raw exceptions
and protocol messages may appear in diagnostics but not as the primary UX.

### 17.4 Application, helper, runtime, and model packaging

Create an Xcode application target that links the existing `VoxInkCapture`
Swift package. The app owns the menu-bar UI, settings, permission flow, capture
selection, captions, and session lifecycle. Development CLIs may remain for
diagnostics but are not the normal product flow.

The app must automatically start, monitor, and stop the local transcription
helper. Lifecycle management must include:

- A readiness probe that avoids unnecessary model loading.
- A version and protocol compatibility handshake.
- Model-loading progress and actionable failure reporting.
- Safe automatic restart after unexpected helper failure.
- Graceful shutdown and final transcript flushing.
- Bounded audio queues, explicit backpressure, and cancellation of obsolete or
  stopped-session inference.

Before beta, benchmark the current Python/Torch runtime and at least one viable
accelerated Apple Silicon path. Select the production runtime using measured
accuracy, latency, memory, power, package size, model compatibility, and
maintenance cost.

If Python remains, bundle a self-contained, reproducible, signed interpreter and
dependencies; never rely on system Python. Runtime and models may live under the
user's Application Support directory and must support resumable downloads where
offered, checksum validation, versioned storage and migration, partial or corrupt
installation detection, preflight disk checks, and removal from settings.

Production IPC must replace broadly discoverable owner credentials with a Unix
domain socket, XPC, a per-launch secret, or an equivalently private mechanism.
Credentials must not appear in command-line arguments or persistent logs.
Messages remain versioned, size-limited, and strictly validated.

### 17.5 Performance and accuracy requirements

Benchmark the production path on every supported hardware tier and set beta
thresholds for model startup, speech-to-first-text, silence-to-final, real-time
factor, peak and steady memory, CPU/GPU use, and battery impact. Detect lag and
never accumulate an unbounded inference queue.

Partial results are replaceable and disposable; finalized results are durable.
The VAD-finalized path remains a fallback. Overlap and deduplication must not
introduce repeated phrases into saved transcripts.

Maintain an evaluation set covering English, Mandarin, code-switching, silence,
noise, music, different microphones and system sources, overlapping microphone
and system speech, and both short utterances and long meetings. A runtime or
model change may ship only after comparison with the recorded accuracy and
latency baseline.

### 17.6 Reliability, privacy, and security requirements

VoxInk must handle helper failure, interrupted app restart, sleep and wake,
microphone or default-device changes, selected-application exit and restart,
permission removal, offline operation, low disk or write failure, malformed or
incomplete protocol messages, and Stop or Quit with pending work without losing
finalized transcript content. Recovery must never overwrite an existing
transcript or audio part. Long-running capture must bound memory and temporary
storage.

In local mode:

- No network request occurs after installation, and this is testable with the
  network disabled.
- Microphone and system audio never leave the Mac.
- Telemetry, diagnostic upload, and crash reporting are absent or opt-in.
- Default logs omit raw audio, transcript text, credentials, and sensitive paths.
- Transcript and retained-audio files use user-only permissions.
- Audio retention is clearly indicated, defaults to enabled, and has a
  pre-session opt-out.
- The app discloses model, transcript, audio, and log locations and can remove
  application-managed data.
- Dependency and model provenance, licenses, integrity checks, and third-party
  notices are documented.

### 17.7 Quality and validation requirements

Pinned Python and Xcode/Swift CI environments must run Python unit and API tests,
Swift unit and contract tests, app/helper protocol compatibility tests, golden
Markdown and event-schema tests, model installation and migration tests, and
release builds with signing checks where credentials permit.

Release validation must include real-model file transcription; microphone,
all-system, selected-application, and combined capture; permission recovery;
helper crash and restart; app restart and transcript recovery; device and
selected-application disappearance; and monitored offline operation.

Before production, complete representative multi-hour meeting tests with
rotation, optional retention, pause/resume, overlapping sources, sleep/wake, and
forced termination. Record memory growth, queue depth, latency, dropped audio,
and recovery outcomes.

### 17.8 Distribution requirements

The production release must include a stable bundle identifier, Developer ID
signing, hardened runtime configuration, correct entitlements and usage strings,
Apple notarization and stapling, a reproducible archive and checksum, a friendly
DMG or equivalent installer, an authenticated update mechanism or documented
manual update path, and release notes, privacy documentation, third-party
notices, support guidance, and uninstall instructions.

The release pipeline must reject unsigned helpers, unverified model artifacts,
incompatible protocol versions, and mismatched build dependencies.

### 17.9 Delivery milestones

1. **Product shell:** add the signed macOS target, menu-bar state and controls,
   source selection, permission onboarding, caption panel, settings, and
   transcript-folder actions. Exit when a developer build completes a session
   without terminal interaction given an installed helper and model.
2. **Managed local engine:** package a self-contained runtime; supervise and
   version the helper; install or import and validate models; secure local
   communication. Exit when a clean Mac reaches ready state without developer
   tools.
3. **Performance and caption quality:** select the production runtime, add Apple
   Silicon acceleration, set device targets, improve partial or stable updates,
   and enforce queue, backpressure, deduplication, and lag behavior. Exit when
   transcription stays responsive and bounded on supported hardware.
4. **Reliability beta:** implement recovery and user-facing errors, CI,
   integration and soak tests, offline validation, and a signed notarized beta.
   Exit when beta users can install, operate, recover, and update without
   developer help.
5. **Production release:** resolve beta blockers; finish licensing, privacy,
   documentation, and support; complete security and performance review; and
   publish the notarized build and update channel. Exit when all release gates
   below pass.

### 17.10 Production release gates

VoxInk is ready to ship only when:

1. A clean supported Mac installs and runs it without a terminal, developer
   tools, or a separately installed Python environment.
2. First-run permissions and model setup succeed or provide recoverable,
   user-readable errors.
3. Microphone, all-system, selected-application, and combined capture work from
   the application UI.
4. Captions stay responsive and transcripts stay durable for the supported
   session duration.
5. Inference queues and temporary storage remain bounded under lag.
6. Forced termination cannot corrupt or overwrite finalized transcript data.
7. Local-only mode produces no network traffic after setup.
8. Local IPC is authenticated or private to the application.
9. Performance, memory, and power targets pass on every supported hardware tier.
10. Automated, real-model integration, and soak tests pass in the pinned release
    environment.
11. The app and every executable helper are signed, hardened, notarized, and
    distributed as verified artifacts.
12. Privacy, licensing, third-party notice, support, update, and uninstall
    documentation are complete.
