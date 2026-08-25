# VoxInk

VoxInk is a privacy-first, local speech-to-text pipeline for microphone and
system audio. The current implementation provides a local Qwen3-ASR service,
live microphone capture, native macOS microphone/system capture, rotating
Markdown transcripts, optional separate/combined audio retention, and a
floating-caption component.

The local API binds only to `127.0.0.1`. The Qwen provider forces Hugging Face
and Transformers offline modes and loads the model exclusively from the local
`Qwen3-ASR-1.7B` directory.

## Current capabilities

- Local batch transcription through HTTP.
- Live PCM streaming through WebSocket.
- Client-side or server-side energy VAD with 700 ms silence finalization.
- Independent microphone and system streams with source-labeled events.
- Manual session start, pause, resume, and stop.
- Markdown rotation at six hours or 5 MiB with recovery markers.
- Optional one-hour rotating microphone/system WAV files.
- Optional combined WAV generation using aligned saturating mixing.
- Native ScreenCaptureKit filtering for all system audio or multiple apps.
- Automatic selected-app reconnection by bundle identifier.
- Minimal always-on-top, click-through-capable macOS caption panel.

Speaker diarization, forced-alignment timestamps, online providers, and polished
UI controls remain extension points rather than completed features.

## Python setup

The tested environment uses Python 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The model weights must already exist locally at `Qwen3-ASR-1.7B/`. VoxInk never
downloads model files while running.

Start the service:

```sh
.venv/bin/python -m service --device cpu
```

The default service URL is `http://127.0.0.1:8765`, and transcripts default to
`~/Documents/VoxInk/`. Check readiness without loading the model:

```sh
curl http://127.0.0.1:8765/health
```

### Batch transcription

```sh
curl -X POST http://127.0.0.1:8765/v1/transcriptions \
  -F file=@samples/asr_en.wav \
  -F session_id=batch-demo \
  -F source=system \
  -F language=en \
  -F end_ms=12000
```

The response contains a normalized transcript event, and the finalized text is
also written under `~/Documents/VoxInk/batch-demo/`.

### Live Python microphone

Install the optional capture dependencies:

```sh
.venv/bin/python -m pip install -r requirements-capture.txt
```

Then run the interactive client while the service is running:

```sh
.venv/bin/python -m apps.python_capture
```

Enter `start`, `pause`, `resume`, `stop`, or `quit`. The default `vad-chunks`
mode sends finalized utterance chunks; `--stream-mode frames` sends continuous
frames and uses the service's VAD.

## Native macOS capture

The Swift package targets macOS 13 or newer and uses only Apple capture APIs:

```sh
cd apps/macos-capture
swift build
swift run voxink-capture --list-apps
swift run voxink-capture --mic
swift run voxink-capture --system-all
swift run voxink-capture --mic --app us.zoom.xos --app com.apple.Safari
```

The service must already be running. macOS will request Microphone and Screen &
System Audio Recording permissions as needed. Selected apps are supplied by
bundle identifier and may be repeated.

The development CLI is not a signed `.app`; macOS privacy grants are more
reliable once the library and caption panel are embedded in a signed app using
the provided plist and entitlements.

## Session and stream protocol

One logical session may contain separate `microphone-1` and `system-1`
WebSockets. Each starts with a versioned `stream.start` JSON message. Every
`audio.frame` or `audio.chunk` JSON header is immediately followed by exactly
one raw PCM s16le binary message. Transcript responses are normalized
`transcript.partial` or `transcript.final` JSON events.

The service tracks active streams per session. Stopping one source does not
finalize the shared session while another source remains active.

See [the Python protocol notes](apps/python_capture/README.md) and
[the macOS capture notes](apps/macos-capture/README.md) for wire examples.

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
documented in `apps/macos-capture/README.md`.

## Architecture and roadmap

The full design, event schema, file formats, implementation phases, and open
extensions are documented in [PROJECT_SPEC.md](PROJECT_SPEC.md).
