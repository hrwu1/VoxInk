# VoxInk Remaining Work

Status: Active roadmap. Last verified against the tree on 2026-09-02.

## Purpose

This is the maintained list of work that remains after the developer workflow,
native capture integration, and the 2026-09-02 feature push. Completed
implementation plans are removed once their durable requirements are reflected
here, in `README.md`, or in tests; the Git history keeps their detail.

`PROJECT_SPEC.md` remains the architecture and data-format reference.
`SHIP_SPEC.md` remains the specification for a signed production release.
`.claude/plans/feature-push.md` records the 2026-09-02 push.

## How this list is ordered

The project is nine days old and has one user. Order follows evidence, not the
shape of the specification:

1. Defects that degrade every session run today.
2. Capabilities the interface advertises but cannot deliver.
3. Verification of the code most likely to break next, which is the code that
   changes most and is tested least.
4. Convenience and completeness.

Work with no observed problem behind it belongs in `Deferred` at the end, not
in a priority tier.

## Verified state

Confirmed by reading the tree and running the checks on 2026-09-02:

- A one-command browser workflow through `./scripts/voxink-dev`, a menu-bar
  application through `--menubar`, and a public `./scripts/voxink` command.
- System audio through Core Audio process taps, with ScreenCaptureKit as the
  fallback. Verified on the target Mac against a muted local player: 837
  frames, 99% nonzero bytes, a finalized transcript. The same run through
  ScreenCaptureKit failed for want of a display to anchor the filter.
- Partial captions while an utterance is open, replaced in place by the
  final; never written to Markdown.
- One labelled channel per selected application, one WAV track per channel,
  and suppression of the microphone's echo of a system channel.
- Timestamped markers, microphone selection, a language override, a Finder
  reveal action, and settings in the browser.
- On-device translation of finalized captions with a local Qwen3 instruct
  model, shown as a second line and quoted in Markdown.
- Speaker diarization after Stop with forced-alignment splitting; per-speaker
  embeddings; an opt-in local registry that names remembered voices.
- Agent access under grants: scopes, application allow-lists, maximum
  duration with auto-stop, retention permission, expiry, single use, an
  approval flow with claim secrets, revocation that stops capture, and an
  audit trail. An MCP server over the shared client.
- Bounded audio retention, recovery of interrupted transcript parts, and the
  level-adaptive speech gate, as before.

Checks, with their results on 2026-09-02:

- `.venv/bin/python -m unittest discover -s tests` — 603 tests, all passing.
- `swift test` in `clients/macos_capture` — 15 tests, all passing.
- `npm test` in `frontend` — 6 tests, passing; `npm run build:local` builds.

## P0: Target-Mac acceptance of the new capture paths

The tap backend delivered real audio from a muted player. What has not been
run on the target Mac, because it needs a person at the keyboard:

- [ ] Per-application taps against a real application (Zoom, Safari): one
      channel per application, reconnection after the application restarts,
      and the `[SYSTEM: Name]` label in the transcript.
- [ ] Microphone plus system with the speakers on, to observe echo
      suppression on real room echo rather than the synthetic test. Tune
      `EchoPolicy.min_correlation` if it fires on the user's own speech.
- [ ] The floating captions and push to talk from the menu-bar application,
      including the Accessibility prompt for pasting.
- [ ] Partial captions with the 1.7B model on MPS: confirm the interval
      stretches rather than queueing work ahead of the final.

Complete when each mode above has been exercised once on the target Mac and
any defect found has a test.

## P1: Speaker recognition quality

The registry, matching, and naming are tested end to end with synthetic
embeddings; real recognition is not. On the one far-field session available,
the dominant speaker's embeddings from separate clips scored cosine 0.27,
under the 0.62 threshold, so recognition may never fire there.

- [ ] Record two short clean sessions of the same person and measure the
      similarity the fallback embedding path produces; set the default
      threshold from that, not from the literature.
- [ ] Prefer the pipeline's own centroid when it is finite and fall back per
      speaker, which is what happens now; check the two agree on a
      multi-speaker session (0.86 on one speaker so far, 0.14 on a speaker
      with short turns).
- [ ] Decide whether system-channel speakers get their own namespace in the
      registry, or stay session-local.

## P1: Translation quality

The 0.6B model translates in about 0.2 s per sentence and follows the prompt,
but it drops clauses and mistranslates jargon ("churn"). Before this is
shown to anyone else:

- [ ] Compare Qwen3-1.7B as the translator on the same sentences; the Mac
      has the memory for it beside the 1.7B ASR model.
- [ ] Carry the vocabulary terms into the translation prompt so names and
      product words survive.

## P1: Cover the code that changes most

- [ ] The recorder hook still owns the capture lifecycle and is untested;
      the caption feed merge was extracted and tested, the rest was not.
- [ ] A focused failure test for the helper dying mid-session.
- [ ] Malformed traffic and model corruption at the service boundary.

## P1: Durable session discovery and recovery

Unchanged from 2026-08-31: `sessions list` reports only sessions the running
helper holds open, and `last_session` in the capture view is the only pointer
to what just finished. Promote when session count or agent use makes
directory listing insufficient.

## P2: Local macOS application

The development menu-bar app exists and drives the helper through the
control API. What remains is production work governed by `SHIP_SPEC.md`:
owning the helper's lifecycle, permissions, Developer ID signing,
notarization, installation, and updates.

## Deferred

**Long-run and replay infrastructure.** Unchanged.

**Remaining public CLI surface.** The `audio`, `config`, and `diagnostics`
command groups, transcript search and export, and derived agent artifacts.

**Owner credential isolation.** A process running as the user can read
`control-endpoint.json` and therefore hold the owner credential. Grants are
enforced for grant tokens; keeping the owner credential out of reach is
signed-application work.

**Offline network assertion.** Unchanged.

## Developer-edition gates

1. One command reaches a ready state without manual port or helper management.
2. All advertised capture modes pass target-Mac acceptance.
3. Capture preserves quiet speech across microphones and input gains, and
   silence produces no text.
4. Local IPC remains authenticated, versioned, and loopback-only.
5. Finalized transcripts survive interruption and recovery without overwrite.
6. Slow inference, long sessions, and a full disk keep memory and storage
   bounded and never cost finalized text.
7. Offline tests detect no unexpected network access.
8. Model, permission, device, helper, and disk failures have useful recovery
   actions.
9. Python, Swift, frontend, real-model smoke, replay, and soak checks pass in
   the supported development environment.

Met: 1, 4, 5, and 9 for the Python, Swift, and frontend suites. Gate 2 is met
for all-system capture on the target Mac and open for per-application and
combined modes. Gate 3 is met in code and fixtures. Open: 6, 7, and 8.
