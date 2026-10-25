# VoxInk

Private, on-device speech-to-text for your Mac. VoxInk turns your microphone,
your system audio, or individual apps such as Zoom into live captions and
Markdown transcripts. Speech recognition runs locally, so your audio never
leaves your computer.

- **Live captions** in a floating panel, updated while a sentence is still
  being spoken.
- **Any audio source:** the microphone, all system audio, or chosen apps, each
  labelled separately in the transcript. It keeps working when your speakers
  are muted.
- **Markdown transcripts** saved to `~/Documents/VoxInk/`, with optional WAV
  recordings beside them.
- **Speaker names:** after a recording, VoxInk can tell speakers apart and
  remember voices you name.
- **Your own words:** teach it names and jargon with a plain-text vocabulary
  file.
- **On-device translation** between English and Chinese.
- **Menu-bar app** with a global shortcut and push-to-talk dictation.
- **Scriptable:** a command-line tool and an MCP server let scripts and AI
  agents use VoxInk, but only with your consent.

VoxInk is an early release that you build from source. macOS is the main
platform. Windows support is experimental; see
[the Windows notes](clients/windows_capture/README.md).

## Requirements

- A Mac with Apple Silicon (M1 or later) running macOS 14 or newer. Recording
  system audio while the speakers are muted needs macOS 14.2 or newer.
- Python 3.12.
- The Xcode Command Line Tools with Swift 6 (`xcode-select --install`), to
  build the menu-bar app and the capture helper.
- About 5 GB of disk space for the default speech model, plus room for your
  transcripts.
- About 4.5 GiB of memory for the default model, or 2.7 GiB for the smaller
  one (see [Models](#models)).

## Install

```sh
git clone https://github.com/hrwu1/VoxInk.git
cd VoxInk
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./scripts/voxink-models download        # the default speech model, 4.4 GiB
./scripts/build-macos-capture           # microphone and system-audio capture
./scripts/build-macos-menubar           # the menu-bar app
```

`voxink-models` shows each model's license and asks before it downloads
anything. See [Models](#models).

## Start VoxInk

```sh
./scripts/voxink-app
```

VoxInk appears in the menu bar. Choose **Audio Source**, then **Start
Recording**, or press **Control + Shift + R** from any app. Press the shortcut
again to stop. The first time each source is used, macOS asks for Microphone or
Screen & System Audio Recording permission.

Transcripts are saved in `~/Documents/VoxInk/`, one folder per session. To
quit, press Ctrl-C in the Terminal window that started VoxInk; open
transcripts are finalized first.

VoxInk has two other interfaces:

```sh
./scripts/voxink-app --web    # a recording console in your browser
./scripts/voxink-app --cli    # type commands such as start, pause, mark, stop
```

## Everyday use

### Choosing what to record

In the menu bar or the browser, pick the microphone, all system audio, or
both. To record specific apps, name them when you start VoxInk. This opens the
Terminal interface:

```sh
./scripts/voxink-app --list-apps                         # find an app's identifier
./scripts/voxink-app --source applications --app us.zoom.xos
./scripts/voxink-app --source microphone --source system
```

Each source is labelled in the transcript (`[MIC]`, `[SYSTEM: Zoom]`). When
your speakers are on, the microphone also picks up what the computer plays.
VoxInk drops that echo so a sentence is not transcribed twice.

### Saving audio

Audio is saved by default as WAV files beside the transcript, with the
microphone and system audio kept separate. Turn this off with **Keep audio** in
the menu, **Save audio** in the browser, or `--no-retain-audio` on the command
line.

### Teaching it your own words

Create `~/Documents/VoxInk/vocabulary.txt` and list the names and terms it
should know:

```
# Blank lines and comments are ignored.
VoxInk
Siobhan

# Fix a word it keeps getting wrong.
shivon => Siobhan
```

Changes apply to the next spoken phrase, even mid-recording. The browser's
**Vocabulary** tab edits the same file. Keep the list short: long lists slow
recognition down.

### Captions, translation, and markers

- **Partial captions** appear while you speak and are replaced by the final
  text.
- **Settings** in the browser chooses the microphone, fixes the language to
  English or Chinese, and turns on translation. Translation needs the optional
  `Qwen3-0.6B` model.
- **Mark** (or the M key in the browser) adds a timestamped marker to the
  transcript.
- **Push to talk:** turn on **Enable push to talk** in the menu, hold ⌃⌥Space
  to speak, and release it to paste the text into the front app.

### Speaker names

With **Identify speakers after Stop** on, VoxInk separates speakers once a
recording ends. This needs the optional speaker models and saved audio. Click
a speaker label in the browser to name it, and tick **Remember this voice** to
have that voice named automatically in later sessions. Named transcripts are
saved as `transcript-with-speakers.md`; the original transcript never changes.

### Scripts and AI agents

`./scripts/voxink` controls a running VoxInk from scripts. Every command can
return JSON:

```sh
./scripts/voxink status
./scripts/voxink capture start --source microphone
./scripts/voxink capture stop --wait
./scripts/voxink transcripts show SESSION_ID
```

`./scripts/voxink-mcp` offers the same operations to MCP clients such as
Claude Code. Agents must ask for permission first. You approve each request in
the browser's **Agent access** tab, every grant is limited in scope and time,
and every action is logged. The full command reference is in
[DEVELOPMENT.md](DEVELOPMENT.md#scripting-and-agents-the-voxink-command).

## Models

VoxInk does not include any model weights. `./scripts/voxink-models` downloads
each model from its publisher's official Hugging Face repository, at the exact
version VoxInk was tested with, and checks every file before use.

```sh
./scripts/voxink-models list                     # what is available and installed
./scripts/voxink-models download                 # the default speech model
./scripts/voxink-models download Qwen3-ASR-0.6B  # any model by name
./scripts/voxink-models download --all
```

| Model | Size | Purpose | License |
| --- | ---: | --- | --- |
| `Qwen3-ASR-1.7B` | 4.4 GiB | Speech recognition (default) | Apache-2.0 |
| `Qwen3-ASR-0.6B` | 1.8 GiB | Faster, lighter speech recognition | Apache-2.0 |
| `Qwen3-0.6B` | 1.4 GiB | English/Chinese translation | Apache-2.0 |
| `pyannote-speaker-diarization-community-1` | 0.03 GiB | Telling speakers apart | CC-BY-4.0 |
| `Qwen3-ForcedAligner-0.6B` | 1.7 GiB | Splitting transcript blocks between speakers | Apache-2.0 |

The pyannote model is gated. Before downloading it, sign in at
[huggingface.co](https://huggingface.co), accept its conditions on
[its model page](https://huggingface.co/pyannote/speaker-diarization-community-1),
then sign in from the Terminal once with `.venv/bin/hf auth login`.

To use the smaller speech model, start VoxInk with
`./scripts/voxink-app --model models/Qwen3-ASR-0.6B`. You can also switch
between installed models under **Model profile** in the browser.

These models are made by third parties and are not covered by VoxInk's MIT
license. Downloading one means you accept its license from its publisher. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Privacy

- Apart from downloading models, VoxInk works offline.
- The helper listens only on your own computer (`127.0.0.1`), and each launch
  has its own private credential.
- Transcripts and audio stay in `~/Documents/VoxInk/`. Remembered voices are
  stored in `~/Library/Application Support/VoxInk/speakers.json`, readable only
  by you.
- Diagnostic logs in `~/Library/Application Support/VoxInk/logs/` never contain
  transcript text, audio, or credentials.

## Troubleshooting

- **"VoxInk cannot start" with missing model files:** run the
  `voxink-models download` command it prints.
- **No system audio, or only the microphone is offered:** build the capture
  helper with `./scripts/build-macos-capture`, then allow it under System
  Settings → Privacy & Security → Screen & System Audio Recording.
- **The microphone goes quiet during calls:** some call apps lower the shared
  microphone volume. VoxInk shows a **Microphone gain** warning in the menu, and
  **Hold microphone gain** keeps it steady.
- **Check your setup without recording:** `./scripts/voxink-app --preflight-only`.

## Contributing

Architecture, protocols, direct debugging, benchmarks, and tests are covered in
[DEVELOPMENT.md](DEVELOPMENT.md). [PROJECT_SPEC.md](PROJECT_SPEC.md) is the
detailed design reference.

## License

VoxInk's source code is released under the [MIT License](LICENSE). Model
weights, dependencies, the bundled React runtime, and the public-domain speech
sample keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
