# Speaker Diarization Implementation Guide

Status: approved design; implementation has not started.

## 1. Goal

Add private, local speaker diarization to VoxInk using pyannote Community-1.
The first complete workflow runs after the user stops a recording, assigns
anonymous session-local speaker labels to microphone speech, and writes derived
speaker artifacts without changing the canonical transcript.

Diarization answers **who spoke when**. It can make a conversation easier to
read and can repair speaker attribution, but it does not improve Qwen's word
recognition by itself.

## 2. Version-one boundaries

Version one must:

- run locally after Stop, not in the live transcription path;
- process retained microphone audio only, which means version one requires
  `audio_saved=true` and `save_separate_audio=true`;
- use pyannote Community-1 through a provider-neutral interface;
- use anonymous labels such as `MIC_SPEAKER_00` and `MIC_SPEAKER_01`;
- preserve raw overlap-aware turns as well as exclusive turns;
- reconcile exclusive turns with finalized transcript timestamps;
- leave uncertain or mixed-speaker transcript segments unlabeled;
- keep every `transcript-part-*.md` file unchanged;
- produce `speaker-turns.json` and `transcript-with-speakers.md` as derived
  artifacts; and
- fail independently so the transcript remains available when diarization
  fails.

Version one does not include:

- speaker names, voiceprints, enrollment, or cross-session identity;
- system-audio diarization;
- live or provisional speaker labels;
- changing VAD or Qwen transcription behavior;
- splitting or retranscribing multi-speaker transcript segments;
- silently rewriting canonical transcript text; or
- a cloud diarization fallback.

These constraints keep the first slice small and protect VoxInk's source
provenance, offline operation, and user-owned artifacts.

## 3. Existing foundation

The project already provides most of the surrounding contract:

- `TranscriptEvent.speaker` is optional.
- Markdown rendering already supports a speaker label.
- capture-derived segment timestamps are session-relative milliseconds;
- `AudioRetentionWriter` can retain aligned 16 kHz PCM WAV parts per source;
- canonical transcript parts are append-only; and
- the shared `.venv` contains the pinned Community-1 runtime dependencies.

The missing responsibilities are the diarization provider, post-Stop job
lifecycle, turn-to-transcript reconciliation, derived artifact writers, and
artifact/API discovery.

## 4. Architecture

```text
microphone-part-*.wav
        |
        v
session audio input ----> isolated diarization worker
                                |
                                v
                     raw + exclusive speaker turns
                                |
transcript-part-*.md ---------->+----> reconciliation
                                         |
                                         +--> speaker-turns.json
                                         +--> transcript-with-speakers.md
```

The service orchestrates the job, but model inference runs in a child process.
The child process uses the same `.venv`; process isolation is for failure and
memory containment, not a second dependency environment. A worker crash,
timeout, or out-of-memory condition must not terminate transcription service
operation or damage session artifacts.

### 4.1 Trigger

Diarization begins only after all of the following are true:

1. the logical session is stopped;
2. every active stream has flushed its final VAD chunk;
3. retained audio writers have closed valid WAV headers;
4. canonical transcript writers have finalized their current parts; and
5. at least one retained microphone WAV part exists.

If separate microphone retention was not enabled when capture started, return
`audio_unavailable`. Do not reconstruct audio from ASR chunks. A later upgrade
may keep an explicitly consented, private working track and delete it after
diarization without retaining it as a user artifact.

Stopping one stream is not enough when another stream in the same session is
still active. The job belongs to the logical session, not an individual
WebSocket.

### 4.2 Source isolation

Never feed `combined-part-*.wav` to the diarizer. Microphone and system audio
have different provenance and may contain the same remote speech at slightly
different times. Mixing them can create echo, duplicate speakers, and false
overlap.

Version one accepts only `microphone-part-*.wav`. A later system-audio provider
may reuse the same contract, but its speaker namespace must remain distinct,
for example `SYSTEM_SPEAKER_00`.

### 4.3 Rotated audio

Community-1 needs session context for stable clustering. Process the complete
microphone timeline as one logical input rather than diarizing each WAV part
independently.

The input assembler must:

- sort parts numerically;
- validate 16 kHz, mono, signed 16-bit PCM input;
- preserve the part timeline, including silence already written for gaps;
- reject overlapping or malformed part ranges rather than guessing; and
- create any joined temporary WAV inside the session's private working
  directory with user-only permissions.

Delete the temporary joined file after success, failure, cancellation, or
service recovery. Original retained audio follows the user's retention policy
and is never deleted by the diarization job.

## 5. Stable domain contracts

Create a provider-neutral package under `voxink_core/diarization/`. The core
contract must not import pyannote so it can be tested without loading Torch.

Recommended immutable models:

```python
@dataclass(frozen=True)
class DiarizationRequest:
    session_id: str
    source: Source
    audio_path: Path
    model_path: Path
    min_speakers: int | None = None
    max_speakers: int | None = None

@dataclass(frozen=True)
class SpeakerTurn:
    speaker_id: str
    start_ms: int
    end_ms: int
    source: Source
    exclusive: bool

@dataclass(frozen=True)
class DiarizationResult:
    model: str
    model_revision: str
    raw_turns: tuple[SpeakerTurn, ...]
    exclusive_turns: tuple[SpeakerTurn, ...]
```

Provider interface:

```python
class DiarizationProvider(Protocol):
    def diarize(self, request: DiarizationRequest) -> DiarizationResult: ...
```

Timestamps crossing the worker boundary are integer milliseconds. Convert
pyannote's floating-point seconds once at the adapter boundary using an
explicit, tested rounding rule. Validate `0 <= start_ms < end_ms` and sort
turns deterministically by start, end, then speaker ID.

The pyannote adapter is the only module allowed to know about `Pipeline`,
Torch device selection, or Community-1 output objects.

## 6. Model acquisition and offline loading

Community-1 is gated. A developer must accept its access conditions and use a
Hugging Face token once to download it. The token is acquisition-only: never
write it to project files, session metadata, logs, command history examples, or
derived artifacts.

The model card identifies Community-1 weights as CC BY 4.0. Record that license
in the manifest and confirm attribution and redistribution obligations before
shipping model weights with VoxInk.

Git LFS is one officially documented acquisition method, but it is not needed
for inference and is not required by VoxInk. The installed Hugging Face CLI can
download the repository directly:

```bash
.venv/bin/hf auth login
.venv/bin/hf download pyannote/speaker-diarization-community-1 \
  --local-dir models/pyannote-speaker-diarization-community-1
```

Before using that location, add it to `.gitignore` while retaining a small,
tracked manifest containing the model ID, resolved revision, license, expected
files, and checksums. Do not commit model weights.

Production inference loads only the local directory:

```python
from pyannote.audio import Pipeline

pipeline = Pipeline.from_pretrained(model_path)
output = pipeline(audio_path)
```

The worker environment must set these controls before importing pyannote:

```bash
PYANNOTE_METRICS_ENABLED=0
HF_HUB_OFFLINE=1
HF_HUB_DISABLE_TELEMETRY=1
```

An offline preflight must fail with a typed `model_unavailable` error when the
local model is absent or incomplete. It must never silently download weights or
fall back to a hosted provider.

Community-1 expects mono 16 kHz audio and exposes both regular
`speaker_diarization` and `exclusive_speaker_diarization`. Preserve both: raw
turns retain overlap information, while exclusive turns are easier to align
with transcript segments.

Start with CPU execution as the correctness baseline. Treat Apple MPS as a
separate benchmarked optimization; do not enable it merely because Torch can
enumerate the device.

## 7. Anonymous speaker labels

pyannote labels are pipeline-local implementation details. Normalize them in a
deterministic order:

1. sort speakers by the start of their first raw turn;
2. break ties by the original pyannote label; and
3. map them to `MIC_SPEAKER_00`, `MIC_SPEAKER_01`, and so on.

The mapping lasts for one derived artifact revision only. It must not be stored
as a voiceprint or reused to infer that speakers in two sessions are the same
person.

If live diarization is added later, it must use opaque provisional IDs and
publish a reconciliation map after Stop. Provisional labels must still remain
outside canonical Markdown.

## 8. Turn-to-transcript reconciliation

Use exclusive turns for the visible one-speaker label and raw turns for overlap
diagnostics. For each finalized transcript segment with interval `[S, E)`:

1. ignore turns from a different source;
2. intersect every exclusive turn with `[S, E)`;
3. sum covered milliseconds by normalized speaker ID;
4. select the speaker with the most covered time;
5. assign the label only when that speaker covers at least 70% of the segment;
6. leave the segment unlabeled on a tie, insufficient coverage, or invalid
   timestamps; and
7. mark the segment `overlap_detected` when two or more raw speakers intersect
   it for a meaningful positive duration.

The 70% threshold is a configurable policy with 0.70 as the version-one
default, not a model constant. Tests should lock down boundary behavior at
69.9%, 70%, ties, gaps, nested overlap, and zero-length intersections.

A transcript segment is a chunker window of up to fifteen seconds, while a
turn in a conversation lasts a second or two, so the rules above cannot label
real dialogue: the window holds both people. Splitting is therefore done, but
only on evidence. The post-Stop worker runs `Qwen3-ForcedAligner-0.6B`
(`qwen_asr`'s forced aligner, Apache-2.0, offline) over each block's audio
and text to time every word; `splitting.py` then places each word against the
exclusive turns and cuts the block into consecutive same-speaker pieces,
snapping a cut to the nearest punctuation within two words. Words the aligner
could not place -- a run stacked on one instant, or timed past the block's
end -- stay unlabeled rather than inheriting a neighbour. A block whose pieces
all agree takes that speaker; one whose pieces disagree is
`multiple_speakers` at block level, with the detail in `pieces`. Text is
never rewritten: a piece is a character range of the canonical block and the
ranges tile it exactly. Without the aligner package, blocks are labeled whole
as above.

### 8.1 Canonical transcript references

Current Markdown does not retain `segment_id`. The version-one reader should
identify an input block with:

- transcript part filename;
- one-based block index within that part;
- source, start, and end timestamps; and
- a SHA-256 digest of the exact canonical block.

The derived artifact also records a SHA-256 digest for every source transcript
part. If a source part changes, the speaker artifact is stale and must be
regenerated rather than partially patched.

## 9. Derived artifacts

Write artifacts atomically: create a private temporary file in the session
directory, flush it, then replace the final path. Never leave a file named
`ready` until both outputs are complete and internally consistent.

### 9.1 `speaker-turns.json`

This is the machine-readable source of truth for diarization output:

```json
{
  "schema_version": 1,
  "status": "ready",
  "session_id": "2026-08-31_14-30-00-000000",
  "source": "microphone",
  "producer": {
    "name": "voxink-pyannote-community-1",
    "version": "1",
    "model_id": "pyannote/speaker-diarization-community-1",
    "model_revision": "resolved-commit"
  },
  "created_at": "2026-08-31T18:42:10Z",
  "source_artifacts": [
    {"path": "microphone-part-001.wav", "sha256": "..."},
    {"path": "transcript-part-001.md", "sha256": "..."}
  ],
  "raw_turns": [
    {
      "speaker_id": "MIC_SPEAKER_00",
      "start_ms": 1250,
      "end_ms": 4820,
      "exclusive": false
    }
  ],
  "exclusive_turns": [],
  "segments": [
    {
      "canonical_ref": {
        "part": "transcript-part-001.md",
        "block": 1,
        "sha256": "..."
      },
      "speaker_id": "MIC_SPEAKER_00",
      "speaker_coverage": 0.86,
      "overlap_detected": false
    }
  ]
}
```

Do not include the Hugging Face token, absolute model-cache paths, raw audio,
or transcript text in this JSON file.

### 9.2 `transcript-with-speakers.md`

This human-readable view copies canonical transcript blocks in their original
order and adds labels only where reconciliation is confident:

```markdown
**[00:00:01.250-00:00:04.820] [MIC] [MIC_SPEAKER_00]**
Could we review the first proposal?
```

Its front matter must identify it as derived, record the producer and creation
time, list source transcript digests, and point to `speaker-turns.json`. It must
not masquerade as a canonical `transcript-part-*.md` file.

## 10. Job lifecycle and failures

Use these externally meaningful states:

- `disabled`: diarization was not requested;
- `waiting_for_stop`: enabled while capture is active;
- `processing`: a worker owns the job;
- `ready`: both derived artifacts passed validation;
- `failed`: canonical artifacts remain usable and a typed failure is recorded;
- `stale`: a source artifact changed after successful processing.

Only one job may own a session/source pair. Repeated requests should return the
current job or regenerate explicitly; they must not start duplicate workers.

Recommended typed errors include:

- `audio_unavailable`;
- `model_unavailable`;
- `invalid_audio`;
- `worker_crashed`;
- `worker_timed_out`;
- `insufficient_disk`;
- `artifact_stale`; and
- `artifact_write_failed`.

Error metadata may contain durations, filenames relative to the session,
versions, and a bounded diagnostic message. It must not contain transcript
text, credentials, or raw audio.

Set an explicit worker timeout derived from audio duration with a generous
minimum. On timeout, terminate the child, clean temporary files, release the
job lock, and keep the canonical transcript untouched.

## 11. Implementation sequence

Each milestone should be a coherent, tested commit. Because the full change
will touch more than three files, implement it in a separate Git worktree and
merge only after the complete slice is verified.

### Milestone 1: model and runtime preflight

- ignore the local Community-1 weight directory;
- add a tracked model manifest format;
- add offline model validation and a one-file CLI smoke check;
- measure CPU latency and memory on the target Mac; and
- evaluate MPS separately without making it the default.

Exit gate: with networking disabled, a retained sample WAV can be processed
from a local model path and missing weights produce `model_unavailable`.

### Milestone 2: provider-neutral core

Add:

- `voxink_core/diarization/models.py` for requests, turns, results, and jobs;
- `voxink_core/diarization/base.py` for the provider protocol;
- `voxink_core/diarization/pyannote.py` for the adapter; and
- focused unit tests using fakes for all non-adapter behavior.

Exit gate: deterministic normalized turns can be produced without importing
pyannote in core contract tests.

### Milestone 3: alignment and artifact writers

Add:

- a canonical Markdown block reader;
- exclusive-turn reconciliation;
- raw-overlap diagnostics;
- JSON schema validation;
- atomic derived artifact writers; and
- stale-source detection using hashes.

Exit gate: fixtures covering one speaker, alternating speakers, overlap, gaps,
ambiguous coverage, rotated transcripts, and changed source parts produce
deterministic artifacts without editing canonical files.

### Milestone 4: post-Stop orchestration

Add a service-owned job manager that:

- waits for logical session completion;
- closes retained audio before dispatch;
- assembles rotated microphone parts;
- launches and supervises one isolated worker;
- enforces timeout and cleanup; and
- records lifecycle state without blocking transcript access.

Exit gate: Stop acknowledges normally, the transcript is immediately readable,
and diarization becomes ready asynchronously. Worker failure leaves the service
healthy and the transcript intact.

### Milestone 5: API, CLI, and UI exposure

Expose capability and job status through typed, additive APIs. Add commands to
request, inspect, retry, and locate diarization artifacts. The UI should show
only the minimal states needed to understand whether speaker processing is
off, running, ready, or failed.

Follow VoxInk's UI approval boundary: prepare isolated static UI alternatives
and obtain explicit approval before production UI implementation.

Exit gate: every surface derives its state from the same job model and no
client reverse engineers artifact filenames.

### Milestone 6: target-Mac evaluation

Evaluate real VoxInk captures across:

- English, Mandarin, and code-switching;
- two, three, and four speakers;
- quiet and noisy rooms;
- close and distant microphones;
- interruptions and overlapping speech;
- short and 30-60 minute sessions; and
- CPU and experimentally supported MPS execution.

Record diarization error rate when labeled reference data exists, plus speaker
count accuracy, time-to-result, real-time factor, peak memory, artifact size,
and failure recovery. Public benchmarks do not replace this test.

## 12. Test matrix

Unit tests:

- provider contract validation and timestamp conversion;
- deterministic anonymous speaker normalization;
- exact 70% coverage boundary and tie handling;
- overlap diagnostics;
- transcript block parsing and hashes;
- artifact schema and atomic replacement; and
- stale-artifact detection.

Integration tests:

- microphone audio is selected and combined/system audio is rejected;
- rotated audio produces one continuous session result;
- Stop dispatches exactly one job after all streams finish;
- transcript access does not wait for diarization;
- worker crash, timeout, cancellation, and restart clean up safely;
- no model download occurs in offline mode; and
- canonical transcript bytes are identical before and after every outcome.

Regression tests:

- run the complete existing Python suite;
- run native capture tests because Stop ordering is involved;
- verify Qwen import and transcription still work in the shared `.venv`; and
- test with pyannote disabled to preserve today's workflow.

## 13. Completion criteria

Version one is complete only when:

- a user can enable diarization for a retained-microphone session;
- Stop returns without waiting for model inference;
- processing uses local Community-1 weights with network access disabled;
- anonymous labels remain stable within the generated revision;
- overlap information survives in `speaker-turns.json`;
- confident transcript segments appear in `transcript-with-speakers.md`;
- ambiguous segments remain unlabeled;
- canonical transcript and retained audio bytes never change;
- all temporary artifacts are cleaned after success and failure;
- worker failures are recoverable and visible;
- the existing Qwen path and full test suite still pass; and
- results and resource use are documented on the target Apple-Silicon Mac.

## 14. Later upgrades

After the post-Stop slice is reliable, the stable interfaces allow these
independent replacements or additions:

- forced word alignment and safe text splitting;
- tentative near-live speaker labels followed by post-Stop reconciliation;
- system-source diarization with a separate speaker namespace;
- a streaming provider such as Sortformer behind the same contract;
- user-approved speaker renaming within one session; and
- a durable session registry that replaces Markdown block references with
  stable event IDs.

None of these upgrades should weaken the offline-only default, source
provenance, anonymous-label default, or immutable canonical transcript.

## 15. Primary references

- [Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [pyannote.audio repository](https://github.com/pyannote/pyannote-audio)
- `PROJECT_SPEC.md` sections 6.4, 8, 9, and 14.6
- `PROJECT_SPEC.md` section 16.3 speaker-recognition quality checkpoint
- `voxink_core/models.py`
- `voxink_core/persistence/audio.py`
- `voxink_core/persistence/markdown.py`

## 16. Speaker identity (implemented 2026-09-02)

Version one's "no voiceprints, no cross-session identity" boundary was lifted
deliberately and narrowly:

- The worker returns pyannote's per-speaker centroid embeddings, re-keyed to
  the anonymous ids. When the pipeline skipped clustering (one speaker) its
  centroids are NaN, and the adapter embeds the speaker's longest exclusive
  turns (up to 60 s) with the pipeline's embedding model instead.
- `speaker-turns.json` gains a `speakers` section with the embedding and an
  optional name, registry id, and similarity. Embeddings never leave the
  session directory unless the user remembers a voice.
- The registry (`speakers.json` in the state directory, 0600) is opt-in per
  voice through "Remember this voice". Matching is cosine similarity above a
  strict threshold; one remembered voice claims at most one session speaker.
- Naming re-renders `transcript-with-speakers.md` from the canonical blocks.
  Canonical parts are still never written.
- System-channel speakers keep the session-local `SYSTEM_SPEAKER_nn` space
  and are not matched against the registry.
