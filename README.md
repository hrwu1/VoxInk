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
- Always-on-top macOS caption panel: frosted, resizable, click-through-capable,
  grouped by speaker turn with names, marks, levels, status, scroll-back, and
  adjustable text; it shrinks to a status bubble when nobody is talking.
- Custom vocabulary that biases recognition and corrects finalized text,
  editable from a browser tab or as a plain file.
- System audio through Core Audio process taps (macOS 14.2+), which work
  while the speakers are muted and need only the System Audio Recording
  permission; ScreenCaptureKit remains the fallback.
- Partial captions while a sentence is still being spoken, replaced in
  place by the final.
- One labelled channel per selected application, and suppression of the
  microphone's echo of what the system just played.
- On-device English/Chinese translation of each finalized caption.
- Timestamped markers, a microphone picker, a language override, and an
  "open folder" action in the browser; a menu-bar app with push to talk.
- Speaker diarization after Stop, with forced-alignment splitting, and a
  local registry that recognizes voices you chose to remember.
- Consent-gated agent access: scoped grants, an audit trail, and an MCP
  server.

Online providers, Windows and Linux capture, and a signed release remain
extension points rather than completed features.

## One-command developer workflow

The tested environment uses Python 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The model weights must already exist locally at `models/Qwen3-ASR-1.7B/`.
VoxInk never downloads model files while running.

The menu-bar application is the default interface. Build it once, then start
the complete local workflow with:

```sh
./scripts/build-macos-menubar
./scripts/voxink-app --device cpu
```

The command verifies the Python environment and local model checksums, checks
disk space, prefers private loopback port `8300` (and chooses the next available
port through `8399` if it is busy), creates a per-launch credential, starts the helper,
negotiates the protocol version, and puts VoxInk in the menu bar. Ctrl-C in the
launching Terminal closes the helper and finalizes open transcript files.
Finalized transcripts default to `~/Documents/VoxInk/`.

`--web` opens the private recording console in the default browser instead, and
so does the bare command when the menu-bar application has not been built yet
(it says so before it does). The page asks for microphone permission, streams
audio only to the supervised loopback helper, displays finalized captions, and
removes its one-time launch token from the browser address immediately.

```sh
./scripts/voxink-app --web --device cpu
```

The browser's **Capture mode** menu supports microphone, all system audio,
selected running applications, and both microphone/system combinations. Every
mode records through the native helper, so build it once with
`./scripts/build-macos-capture` before recording.

For selected-application capture, open the target app, choose it from the list,
and press **Refresh** if it was opened after VoxInk. Start, pause, resume, and
stop work from the same browser controls for every mode.

For the installed Diart experiment, restart `./scripts/voxink-app`, choose a
mode that includes the microphone, enable **Live speakers (experimental)**,
and press **Start recording**. The checkbox is off by default. Wait for model
loading, then speak normally; **Speaker 1**, **Speaker 2**, overlap, and
unassigned labels appear in the live captions. These labels are provisional,
and the first captions take longer while Qwen loads. Stop also cancels startup.
The option uses the existing isolated Diart runtime without installing anything;
it explains when that runtime is unavailable. It works with **Save audio** off.
Post-Stop **Identify speakers** remains a separate option for retained audio.
Provisional live labels are not written into the canonical transcript.

For typed controls in the launching Terminal, select the CLI interface
explicitly:

```sh
./scripts/voxink-app --cli --device cpu
```

This opens the `voxink shell`: every line you type is a `voxink` command
(`capture start --source system`, `speakers list`, `help`), and bare `start`,
`pause`, `resume`, `stop`, `mark [note]`, and `status` stand for their
`capture` commands. Captions print between prompts as they arrive. Name
`--source` values to choose what a bare `start` records. `quit` or Ctrl-C
stops an open capture and the helper, and the saved transcript path is
printed on the way out. The same shell opened by hand in another window
(`./scripts/voxink shell`) attaches to the running helper and leaves the
capture running when it exits.

Every interface records through a capture backend the helper launches. The
Swift `voxink-capture` (built by `./scripts/build-macos-capture`) serves the
microphone, all system audio, and selected applications. Where it is not
built, `voxink-app` says so and the helper launches the portable Python
backend instead (`scripts/voxink-mic-capture`), which serves the microphone
only and needs the capture dependencies:

```sh
.venv/bin/python -m pip install -r requirements-capture.txt
```

With that backend the browser and Terminal offer microphone modes only, and a
system source is refused with the build instruction.

### Keeping the audio

Audio retention is on by default. The browser exposes a **Save audio** control
before each session, and Terminal workflows save captured audio as rotating WAV
parts beside the transcript. Use `--no-retain-audio` to start either interface
with retention disabled:

```sh
./scripts/voxink-app
./scripts/voxink-app --cli
./scripts/voxink-app --no-retain-audio
```

Microphone and system audio are saved as separate tracks so their sources stay
distinguishable. The setting reaches whichever interface you selected. When
retention is enabled at launch, preflight requires 2 GiB free instead of 512 MiB
before it will start.

### Teaching it your own words

A speech model cannot guess a personal name, a product name, or in-house jargon
it never saw in training. Create `vocabulary.txt` in the output directory
(`~/Documents/VoxInk/vocabulary.txt` by default) and list the words that matter:

```
# Blank lines and comments are ignored.
VoxInk
ScreenCaptureKit
Hrwu

# When biasing alone is not enough, correct what the model actually produces.
hallow => Hrwu
```

A bare line becomes recognition context: it is handed to the model before it
transcribes, which makes an unfamiliar word possible to recognize in the first
place. A `misheard => Correct` line additionally rewrites finalized text, for
the words biasing alone still gets wrong; its right-hand side is biased too, so
one rule covers both. Matching is case-insensitive and never cuts an ASCII word
in half, so `hallow => Hrwu` leaves "Halloween" alone.

The file is re-read whenever it changes, so an edit takes effect on the next
spoken phrase without restarting VoxInk. It is optional — with no file, nothing
about transcription changes. Keep the list to the words you actually need: it
is sent with every chunk of audio, so a very long list costs recognition speed.

Use a file somewhere else with:

```sh
./scripts/voxink-app --vocabulary ~/Documents/work-terms.txt
```

The browser console edits the same file under its **Vocabulary** tab, so a text
editor is optional. The tab shows the file exactly as it is on disk, comments
included, and lists beside it the terms and correction rules the helper actually
parsed — including any terms past the size limit, which are struck through
because they are not being sent to the model. **Save** writes the file and the
next spoken phrase uses it, even in the middle of a recording.

### Native system and application capture

Build the native capture helper after cloning and whenever its Swift source
changes:

```sh
./scripts/build-macos-capture
```

This creates a background-only development app with the stable identity macOS
uses for Microphone and Screen & System Audio Recording permissions.

Then the same `voxink-app` supervisor can run every native source combination:

```sh
# Show bundle identifiers for currently running applications.
./scripts/voxink-app --list-apps

# All system audio.
./scripts/voxink-app --source system

# Microphone and all system audio together.
./scripts/voxink-app --source microphone --source system

# One or more selected applications.
./scripts/voxink-app --source applications \
  --app com.apple.Safari --app us.zoom.xos

# Microphone and selected applications together.
./scripts/voxink-app --source microphone --source applications \
  --app com.apple.Safari
```

Naming sources on the command line selects the CLI shell, the one interface
that takes its sources from the flags; the menu bar and the browser choose
theirs from their own controls. Every route records through the helper, which
launches the capture backend; the floating caption panel belongs to the
menu-bar app, and the shell prints captions as lines. `voxink-app` supplies
the private loopback endpoint and per-launch credential, monitors the helper,
and shuts everything down on Ctrl-C. macOS requests Microphone and/or Screen &
System Audio Recording permission when each protected source is first used.

Run only the startup checks with:

```sh
./scripts/voxink-app --preflight-only
```

The checks match the capture backend the helper will launch: the Swift build
where it exists, otherwise the portable Python backend and its `sounddevice`
dependency.

Private operational diagnostics are stored under
`~/Library/Application Support/VoxInk/logs/`. They contain process state and
protocol metadata, never transcript text, raw audio, or the launch credential:

- `developer-<stamp>-<pid>.jsonl` and `.stderr.log`: the launcher's view of
  the service process, and everything the service wrote to stderr.
- `service-<stamp>-<pid>.jsonl`: stream and helper lifecycle events (a stream
  opened, released, or rejoined; a grace period expiring; live speakers
  stopping; the capture helper starting and exiting).
- `capture-<session>.log`: every status line the capture helper wrote while
  that session ran, timestamped.

### A recording is never lost

Audio is kept at every layer between the microphone and the transcript:

- **Live speakers is optional.** If the experimental diarizer rejects a frame
  or its worker dies, the service drops it for that session, tells the client
  with a `live_speakers_stopped` notice, and keeps transcribing on the same
  socket. A stream that reopens afterwards is accepted without it.
- **A dropped socket is not a Stop.** The service holds the session for a
  60 s grace period; the helper reconnects with exponential backoff for as
  long as the recording runs, queues the frames captured meanwhile, and
  replays them in capture order once it is back, so the transcript has no gap.
- **What cannot be delivered is spooled.** If the queue overflows (about eight
  minutes), the service has finalized the session, or Stop arrives while the
  service is unreachable, the helper writes the audio to
  `<stream>-spool-NNN.wav` beside the transcript and reports `audio_spooled`;
  the capture status lists these files under `last_session.spooled_audio`.
  Transcribe one later with `voxink transcribe <file>`.
- **Degradation is visible.** The menu bar shows the helper's notice under the
  Live badge and in its menu, the browser shows it as a message, and Stop
  still finalizes the transcript when the helper itself has died.

### Live captions, translation, markers, and settings

Captions appear while you are still speaking: the helper re-transcribes the
open utterance about once a second (less often when inference is slower than
that) and sends a `transcript.partial` under a stable segment id; the final
replaces it in place. Partial text never reaches Markdown. Turn it off with
`--no-partial-captions`.

The **Settings** button (⌘ in the top bar) chooses the microphone, fixes the
language to English or Chinese instead of detecting it, and turns on
translation. With `models/Qwen3-0.6B` installed (Apache-2.0; a manifest is
tracked, the weights are not), each finalized caption is translated into the
other language on this Mac, shown as a second line, and quoted under the block
in Markdown as `> [00:00:03.200 · zh] …`. The 0.6B model is fast (about 0.2 s
per sentence on Apple Silicon) but small; expect readable, not polished,
translations. Point `--translation-model` at a larger local instruct model
for better ones.

**Mark** (or the M key) writes `**[00:12:03.200] [MARK]**` into the transcript
at the current moment. **Open folder** on the last-session strip reveals the
session in Finder. Both are also CLI commands (`capture mark`,
`transcripts open`).

### System audio, channels, and echo

Selected-application capture opens one Core Audio process tap per application,
so the transcript labels each block with the application it came from
(`[SYSTEM: Zoom]`), retained audio keeps one WAV track per application
(`system-us.zoom.xos-part-001.wav`), and the browser shows the application
name on the caption. All-system capture is one tap that excludes VoxInk's
own audio. Taps intercept audio before the output device, so they keep
delivering while the speakers are muted; the run that verified this captured
837 frames at 99% nonzero bytes from a muted player, where ScreenCaptureKit
delivered silence.

When the speakers are on, the microphone hears the remote side a few tens of
milliseconds after the system track played it. VoxInk compares each
microphone utterance with what the system channels played over the same
window and drops the ones that are a delayed copy (`audio.echo_suppressed`
on the stream), so the same sentence is not transcribed twice under two
labels. Turn it off with `--no-echo-suppression`.

### The menu-bar application

```sh
./scripts/build-macos-capture
./scripts/build-macos-menubar
./scripts/voxink-app
```

This is what `voxink-app` runs by default; `--menubar` asks for it explicitly,
which reports the missing build instead of falling back to the browser. The
app is its own Swift package in `voxink_menubar/`, a control surface beside
`voxink_cli` and `voxink_mcp`; it depends on the capture package only for the
floating caption panel.

The menu bar item starts and stops capture, pauses, marks, shows or hides the
floating captions, and opens the transcript folder. **Control + Shift + R**
starts a normal recording from any app and stops/finalizes it on the next press
(also when paused). The **Audio Source** submenu selects Microphone, System
audio, or both without starting capture, and remembers the selection across
launches (microphone on first use). Source selection is disabled during a session
or push-to-talk. The **Start Recording / Stop Recording** control and `⌃⇧R` use
that selection and the saved recording options. Holding the keys or pressing during a
pending command does not queue extra actions. The shortcut is inactive while
push-to-talk is enabled; turn push-to-talk off to record normally. VoxInk and its
helper must already be running. If another app prevents shortcut registration,
the menu reports it; resolve the conflict and restart VoxInk to retry.
**Keep audio** and
**Identify speakers after Stop** are the browser's checkboxes of the same
names; they apply to Start Recording, are on by default, persist across
launches, and speaker identification needs kept microphone audio. **Live
speakers (experimental)** is the browser's checkbox of the same name: it is off
by default, persists across launches, is offered only when the Diart experiment
runtime is available (the menu explains why in its tooltip when it is not), and
is requested only for Start items that include the microphone. While it runs,
the menu reports loading, on, or the failure, and the floating captions label
each turn **Speaker 1 · provisional**, **… · overlap · provisional**, or
**Unassigned · provisional**. Those labels are never written into the
transcript. **Enable push to talk**
holds a microphone session open; hold ⌃⌥Space to speak, release to have the
finalized text pasted into the front application (with Accessibility access;
otherwise it waits on the clipboard). The app is a control surface over the
same local API the browser uses and never captures audio itself.

### Speakers that keep their names

With **Identify speakers** on, diarization runs after Stop and labels
anonymous speakers. Click a speaker label in the browser to name it; tick
**Remember this voice** and the speaker's embedding is stored in
`~/Library/Application Support/VoxInk/speakers.json` (mode 0600, this Mac
only). Later sessions compare their speakers against remembered voices and
name a match automatically; one remembered voice claims at most one speaker
per session, and nothing below the similarity threshold
(`--speaker-threshold`, default 0.62) is named. Names appear in
`transcript-with-speakers.md`; the canonical transcript never changes.
`voxink speakers list|name|rename|forget` does the same from the CLI.

Once a voice is remembered it can also be named **live**: each stream keeps
a rolling mean of its recent utterance embeddings (about the last 20 s of
speech), and once four seconds of one voice have accumulated the mean is
compared against the registry with the same threshold. A match names the
caption in the feed, the floating panel, and the transcript's `[MIC] [Name]`
tag; the utterance itself must also resemble the matched voice, so a brief
interjection by someone else stays anonymous. A single utterance is never
enough on its own (a 2 s clip scores about 0.4 against the same voice on the
reference sample; 4 s accumulated scores 0.75), and a system stream where
several people alternate blurs its mean and stays unnamed. The embedding
model loads on the first utterance after a voice is enrolled, so that one
caption arrives a couple of seconds late. `live_speaker_names` in
`ServiceSettings` turns it off.

Recognition quality is unverified on real multi-speaker sessions: on the one
far-field session available, embeddings of the dominant speaker across
separate clips scored 0.27, below the threshold, so recognition may stay
silent there. The threshold is strict on purpose.

### Agent access under consent

Agents act under a **grant** the owner issues or approves in the browser's
**Agent access** tab. A grant carries scopes, an optional application
allow-list, a maximum recording length (the capture stops when it runs out),
whether audio may be kept, an expiry, and single use. A grant token
authenticates the control API; a call that lacks a scope returns
`approval_required` together with a pending request the tab shows, and a
claim secret only the caller sees. An agent that holds no credential at all
gets the same answer rather than a bare refusal, so a first run reaches the
consent flow from the error it actually receives. `voxink permissions scopes`
lists what a grant may carry; `capabilities` reports `credential` and
`permitted_scopes`, which is what the caller may actually do. Revoking a grant
stops any capture it started. Every control mutation writes one line to
`audit.jsonl` (no transcript text, no credentials).

```sh
./scripts/voxink permissions request --scope capture.system --max-minutes 45 --client "Claude Code"
# approve it in the Agent access tab, then
./scripts/voxink permissions claim REQUEST_ID --claim-secret ... --out ~/.voxink-token --wait
VOXINK_GRANT_TOKEN_FILE=~/.voxink-token ./scripts/voxink capture start --source system
```

`scripts/voxink-mcp` serves the same operations to an MCP client over stdio
(`voxink_capture_start`, `voxink_capture_events` with a long-poll wait so an
agent can follow a meeting as it happens, `voxink_transcript_show`, and the
request/claim consent tools). Point it at a token file with `--token-file`
or the environment variable above; without one it can only ask. A refused tool
files the request for the agent and returns its `request_id` and
`claim_secret`, naming only the scopes that tool needs — starting a microphone
capture asks for `capture.microphone`, not for every capture scope — so the
agent's next step is to have the owner approve it and call
`voxink_permissions_claim`.

## Scripting and agents: the `voxink` command

`scripts/voxink-app` is the interactive launcher. `scripts/voxink` is the
public command for scripts and AI agents: it is a thin client of the running
helper's local control API, so it never re-implements capture, transcription,
persistence, or model management, and it never scans the transcript folder
itself. `PROJECT_SPEC.md` section 14 is the full contract.

Start a helper in one Terminal, then use `voxink` from another:

```sh
./scripts/voxink-app                 # publishes a private control endpoint
./scripts/voxink status
```

The launcher writes the loopback address and its credential to
`~/Library/Application Support/VoxInk/control-endpoint.json`, readable only by
you, and removes it when the helper stops. The credential never appears in a
command line, so it stays out of process listings and shell history. Point the
CLI at a different helper with `VOXINK_CONTROL_ENDPOINT`.

### Machine-readable output

Every command supports `--format text|json|jsonl`. Machine-readable results use
one versioned envelope:

```sh
./scripts/voxink --format json capabilities
```

```json
{"schema_version": "1", "ok": true, "command": "capabilities", "data": {}, "warnings": []}
```

Failures carry a stable code instead of prose, so a consumer never parses an
error message:

```json
{"schema_version": "1", "ok": false, "command": "capture.start",
 "error": {"code": "approval_required", "message": "...", "retryable": true,
           "request_id": "req_123", "details": {}}}
```

Structured results go to stdout and human diagnostics to stderr. Exit codes are
part of the contract:

| Exit | Error codes |
| --- | --- |
| 0 | success |
| 1 | `internal_error` |
| 2 | `invalid_request` |
| 3 | `not_ready` |
| 4 | `not_found` |
| 5 | `permission_denied`, `approval_required` |
| 6 | `invalid_transition`, `conflict` |
| 7 | `capture_unavailable`, `model_busy`, `disk_low` |
| 8 | `timeout` |

Every command also accepts `--schema-version`, `--timeout`, `--request-id`,
`--idempotency-key`, and `--no-input`, before or after the command name.

### Available commands

```sh
./scripts/voxink capabilities                 # supported, available, permitted features
./scripts/voxink status                       # readiness and capture state
./scripts/voxink doctor                       # actionable local failures
./scripts/voxink devices list                 # microphone devices
./scripts/voxink applications list            # capturable applications

./scripts/voxink capture start --source microphone --source system
./scripts/voxink capture start --source applications --app us.zoom.xos \
    --device BuiltInMicrophoneDevice --language zh --translate
./scripts/voxink capture watch                # stream events until the session ends
./scripts/voxink capture mark "decision"      # timestamped marker
./scripts/voxink capture pause
./scripts/voxink capture resume
./scripts/voxink capture stop --wait
./scripts/voxink transcripts open             # reveal the last session in Finder

./scripts/voxink speakers list
./scripts/voxink speakers name SESSION_ID MIC_SPEAKER_00 "Wei" --remember

./scripts/voxink permissions scopes
./scripts/voxink permissions request --scope transcripts.read --client "agent"
./scripts/voxink permissions claim REQUEST_ID --claim-secret S --out TOKEN_FILE
./scripts/voxink permissions list             # owner only
./scripts/voxink permissions audit --limit 50 # owner only

./scripts/voxink sessions list
./scripts/voxink sessions show SESSION_ID
./scripts/voxink transcribe recording.wav --language en
./scripts/voxink transcripts list
./scripts/voxink transcripts show SESSION_ID [--part 2]

./scripts/voxink models list
./scripts/voxink models select Qwen3-ASR-0.6B
./scripts/voxink vocabulary show
./scripts/voxink vocabulary replace --file vocabulary.txt
```

`capture watch` reports an absolute `next_cursor` with every event, so an agent
that reconnects with `--after` resumes without replaying or skipping events.

### Known limits of this slice

- `sessions list` reports only sessions the running helper holds open. Completed
  and interrupted sessions are discoverable through `transcripts list` until the
  durable session registry lands.
- Scoped grants are enforced for grant tokens. The owner credential in
  `control-endpoint.json` still authorizes every scope, and a process running
  as the same user can read that file; the signed application will keep that
  credential to itself. `capabilities` reports which credential a call holds.
- The `audio`, `config`, and `diagnostics` command groups, transcript search
  and export, and derived agent artifacts are deferred.
- `capture start` reaches the control plane through `/v1/ui/capture/*`, a
  UI-namespaced route. Extracting a domain capture route is deferred with the
  durable session registry (`PROJECT_SPEC.md` section 14.2).

## Direct service debugging

Normal use should go through `scripts/voxink-app`. For protocol debugging, set
a temporary credential before starting the helper directly:

```sh
export VOXINK_AUTH_SECRET="replace-with-a-random-development-secret"
.venv/bin/python -m services.transcription --device cpu
```

The default direct-debug URL is `http://127.0.0.1:8300`. `/health` exposes only
minimal readiness and compatibility metadata; all session HTTP routes and
WebSocket upgrades require `Authorization: Bearer $VOXINK_AUTH_SECRET`.

### Batch transcription

```sh
curl -X POST http://127.0.0.1:8300/v1/transcriptions \
  -H "Authorization: Bearer $VOXINK_AUTH_SECRET" \
  -F file=@samples/asr_en.wav \
  -F session_id=batch-demo \
  -F source=system \
  -F language=en \
  -F end_ms=12000
```

The response contains a normalized transcript event, and the finalized text is
also written under `~/Documents/VoxInk/batch-demo/`.

### Direct portable microphone capture

With the direct service running and the credential still exported, the
portable backend streams the microphone until Ctrl-C:

```sh
./scripts/voxink-mic-capture --mic --endpoint ws://127.0.0.1:8300/v1/sessions/demo/stream --session-id demo
```

It accepts the same command line and signals as the Swift `voxink-capture`
(`--list-devices`, `--list-sources`, `--mic-device`, `--language`,
`--retain-audio`; SIGUSR1 pauses, SIGUSR2 resumes, SIGINT stops), which is what
lets the helper launch either. It sends frames; the helper's VAD forms
utterances.

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

### Efficient model profile

The optional local `Qwen3-ASR-0.6B` profile uses the same provider and remains
offline. The 1.7B profile remains the default; select the smaller model with:

```sh
./scripts/voxink-app --model models/Qwen3-ASR-0.6B --device auto
```

When both model packages are installed, the browser control surface also lists
them under **Model profile**. Selection applies to the current helper run and is
locked while a recording is active. Every change is verified against the local
model manifest before the next provider is activated.

Measured on the same Apple Silicon Mac and English fixture, with two timed runs
per utterance length:

| Model and runtime | Load | Peak memory | 2 s RTF | 5 s RTF | 10 s RTF | 15 s RTF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1.7B CPU | 12.23 s | 8996 MiB | 0.590x | 0.382x | 0.302x | 0.277x |
| 0.6B CPU | 4.37 s | 5477 MiB | 0.270x | 0.192x | 0.141x | 0.137x |
| 1.7B MPS | 9.51 s | 3781 MiB | 0.159x | 0.116x | 0.091x | 0.082x |
| 0.6B MPS | 4.87 s | 2794 MiB | 0.095x | 0.072x | 0.061x | 0.057x |

On MPS, 0.6B used 26% less peak memory than 1.7B, loaded 49% faster,
and reduced steady-state inference latency by roughly 30–40%. Its downloaded
weights are 1.75 GiB instead of 4.38 GiB. These measurements establish the
hardware benefit only; choosing a new default still requires representative
English, Mandarin, and code-switch accuracy evaluation.

Raw results are in `benchmarks/cpu-0.6b.json`,
`benchmarks/mps-baseline.json`, and `benchmarks/mps-0.6b.json`.

## Tests

Run all fast Python tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Frontend unit tests (every `voxink_web/tests/*.test.ts`) and the static build:

```sh
cd voxink_web && pnpm install --offline && npm test && npm run build:local
```

Run the real local model smoke test:

```sh
.venv/bin/python transcribe.py samples/asr_en.wav --device cpu --max-new-tokens 96
```

Swift build and test instructions, including local toolchain limitations, are
documented in `clients/macos_capture/README.md`.

## Architecture and roadmap

- [PROJECT_SPEC.md](PROJECT_SPEC.md) is the canonical architecture,
  event-schema, file-format, active-roadmap, and production-shipping reference.
- [.claude/plans/public-cli.md](.claude/plans/public-cli.md) records the
  public CLI review, its accepted deviations, and what it defers.
