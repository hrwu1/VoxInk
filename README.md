# VoxInk

VoxInk is a privacy-first, local speech-to-text pipeline for microphone and
system audio. The current implementation provides a local Qwen3-ASR service,
live microphone capture, native macOS microphone/system capture, rotating
Markdown transcripts, optional separate/combined audio retention, and a
floating-caption component.

The local API binds only to `127.0.0.1`. The Qwen provider forces Hugging Face
and Transformers offline modes and loads the model exclusively from the local
`models/Qwen3-ASR-1.7B` directory.

## Current capabilities

- Local batch transcription through HTTP.
- Live PCM streaming through WebSocket.
- Client-side or server-side energy VAD with 700 ms silence finalization.
- Independent microphone and system streams with source-labeled events.
- Manual session start, pause, resume, and stop.
- Markdown rotation at six hours or 5 MiB with recovery markers.
- Optional one-hour rotating microphone/system WAV files, off unless requested.
- Optional combined WAV generation using aligned saturating mixing.
- Native ScreenCaptureKit filtering for all system audio or multiple apps.
- Automatic selected-app reconnection by bundle identifier.
- Minimal always-on-top, click-through-capable macOS caption panel.

Speaker diarization, forced-alignment timestamps, online providers, and polished
UI controls remain extension points rather than completed features.

## One-command developer workflow

The tested environment uses Python 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The model weights must already exist locally at `models/Qwen3-ASR-1.7B/`.
VoxInk never downloads model files while running.

The browser recording interface is the default. Start the complete local
workflow with:

```sh
./scripts/voxink-dev --device cpu
```

The command verifies the Python environment and local model checksums, checks
disk space, chooses a private loopback port, creates a per-launch credential,
starts the helper, negotiates the protocol version, and opens a private recording
console in the default browser. The page asks for microphone permission, streams
audio only to the supervised loopback helper, displays finalized captions, and
removes its one-time launch token from the browser address immediately. Ctrl-C
in the launching Terminal closes the helper and finalizes open transcript files.
Finalized transcripts default to `~/Documents/VoxInk/`.

The browser's **Capture mode** menu supports microphone, all system audio,
selected running applications, and both microphone/system combinations. Build
the native helper once with `./scripts/build-macos-capture` to enable every mode.
For selected-application capture, open the target app, choose it from the list,
and press **Refresh** if it was opened after VoxInk. Start, pause, resume, and
stop work from the same browser controls for every mode.

For interactive Terminal microphone controls, install the capture dependencies
and select the Terminal interface explicitly:

```sh
.venv/bin/python -m pip install -r requirements-capture.txt
./scripts/voxink-dev --terminal --device cpu
```

Enter `start`, `pause`, `resume`, `stop`, or `quit`. Ctrl-C shuts down both
capture and the helper, and the saved transcript path is printed when capture
exits. The former explicit `--web` spelling remains supported for compatibility.

### Keeping the audio

Audio retention is off by default. Add `--retain-audio` to any workflow to save
the captured audio as rotating WAV parts beside the transcript:

```sh
./scripts/voxink-dev --retain-audio
./scripts/voxink-dev --terminal --retain-audio
./scripts/voxink-dev --source microphone --source system --retain-audio
```

Microphone and system audio are saved as separate tracks so their sources stay
distinguishable. The flag reaches whichever interface you selected, and preflight
requires 2 GiB free instead of 512 MiB before it will start.

### Native system and application capture

Build the native capture helper after cloning and whenever its Swift source
changes:

```sh
./scripts/build-macos-capture
```

This creates a background-only development app with the stable identity macOS
uses for Microphone and Screen & System Audio Recording permissions.

Then the same `voxink-dev` supervisor can run every native source combination:

```sh
# Show bundle identifiers for currently running applications.
./scripts/voxink-dev --list-apps

# All system audio.
./scripts/voxink-dev --source system

# Microphone and all system audio together.
./scripts/voxink-dev --source microphone --source system

# One or more selected applications.
./scripts/voxink-dev --source applications \
  --app com.apple.Safari --app us.zoom.xos

# Microphone and selected applications together.
./scripts/voxink-dev --source microphone --source applications \
  --app com.apple.Safari
```

Native workflows selected with command-line `--source` flags use the Swift
floating-caption panel. The default browser workflow can select the same modes
from its Capture mode menu. `voxink-dev` supplies the private loopback endpoint and per-launch
credential, monitors the native capture process and transcription helper, and
shuts both down on Ctrl-C. macOS requests Microphone and/or Screen & System Audio
Recording permission when each protected source is first used.

Run only the startup checks with:

```sh
./scripts/voxink-dev --preflight-only
```

The checks match the interface you select, so add `--terminal` when you intend
to use the Terminal controls; only then are the capture dependencies verified.

Private operational diagnostics are stored under
`~/Library/Application Support/VoxInk/logs/`. They contain process state and
protocol metadata, never transcript text, raw audio, or the launch credential.

## Direct service debugging

Normal use should go through `scripts/voxink-dev`. For protocol debugging, set
a temporary credential before starting the helper directly:

```sh
export VOXINK_AUTH_SECRET="replace-with-a-random-development-secret"
.venv/bin/python -m services.transcription --device cpu
```

The default direct-debug URL is `http://127.0.0.1:8765`. `/health` exposes only
minimal readiness and compatibility metadata; all session HTTP routes and
WebSocket upgrades require `Authorization: Bearer $VOXINK_AUTH_SECRET`.

### Batch transcription

```sh
curl -X POST http://127.0.0.1:8765/v1/transcriptions \
  -H "Authorization: Bearer $VOXINK_AUTH_SECRET" \
  -F file=@samples/asr_en.wav \
  -F session_id=batch-demo \
  -F source=system \
  -F language=en \
  -F end_ms=12000
```

The response contains a normalized transcript event, and the finalized text is
also written under `~/Documents/VoxInk/batch-demo/`.

### Direct live Python microphone debugging

With the direct service running and the credential still exported:

```sh
.venv/bin/python -m clients.python_capture
```

Enter `start`, `pause`, `resume`, `stop`, or `quit`. The default `vad-chunks`
mode sends finalized utterance chunks; `--stream-mode frames` sends continuous
frames and uses the service's VAD.

## Native macOS capture

The Swift package targets macOS 13 or newer and uses only Apple capture APIs:

```sh
cd clients/macos_capture
swift build
swift run voxink-capture --list-apps
swift run voxink-capture --mic
swift run voxink-capture --system-all
swift run voxink-capture --mic --app us.zoom.xos --app com.apple.Safari
```

The service must already be running. macOS will request Microphone and Screen &
System Audio Recording permissions as needed. Selected apps are supplied by
bundle identifier and may be repeated.

For direct native debugging, build the development app first and run its inner
executable. The one-command workflow selects that executable automatically.

## Session and stream protocol

One logical session may contain separate `microphone-1` and `system-1`
WebSockets. Each starts with a versioned `stream.start` JSON message. Every
`audio.frame` or `audio.chunk` JSON header is immediately followed by exactly
one raw PCM s16le binary message. Transcript responses are normalized
`transcript.partial` or `transcript.final` JSON events.

The service tracks active streams per session. Stopping one source does not
finalize the shared session while another source remains active.

See [the Python protocol notes](clients/python_capture/README.md) and
[the macOS capture notes](clients/macos_capture/README.md) for wire examples.

## Measured performance

`benchmarks/cpu-baseline.json` records the current CPU baseline, produced by:

```sh
./scripts/voxink-bench --device cpu --json benchmarks/cpu-baseline.json
```

On Apple Silicon with the CPU-only Torch build, transcription is comfortably
faster than real time at every utterance length the VAD produces:

| Utterance | Median inference | Real-time factor |
| --- | --- | --- |
| 2 s | 1.18 s | 0.59x |
| 5 s | 1.91 s | 0.38x |
| 10 s | 3.02 s | 0.30x |
| 15 s | 4.16 s | 0.28x |

Model load takes about 12 s and peak resident memory reaches roughly 9 GiB, so
memory headroom — not inference speed — is the binding constraint on the current
runtime. Accelerated Apple Silicon inference is an optimization, not a
prerequisite for live captions. Re-run the benchmark and compare against this
file before changing the model or runtime.

## Tests

Run all fast Python tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Run the real local model smoke test:

```sh
.venv/bin/python transcribe.py samples/asr_en.wav --device cpu --max-new-tokens 96
```

Swift build and test instructions, including local toolchain limitations, are
documented in `clients/macos_capture/README.md`.

## Architecture and roadmap

- [PROJECT_SPEC.md](PROJECT_SPEC.md) is the architecture, event-schema, and
  file-format reference.
- [REMAINING_WORK.md](REMAINING_WORK.md) is the maintained developer roadmap.
- [SHIP_SPEC.md](SHIP_SPEC.md) defines the signed production release.
