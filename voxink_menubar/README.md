# VoxInk menu-bar application

`voxink-menubar` is a control surface over the helper's HTTP API, the native
sibling of the `voxink` command and the MCP server. `voxink-dev` launches it as
its default interface with `VOXINK_SERVICE_URL`, `VOXINK_AUTH_SECRET`, and
`VOXINK_OUTPUT_ROOT` in its environment.

It starts, pauses, marks, and stops capture (optionally keeping the audio and
identifying speakers after Stop, remembered in UserDefaults; push-to-talk
sessions never keep audio), shows the floating captions (with the session
state and marks; hiding the panel from its own toolbar unchecks the menu
item), opens the transcript folder, and offers push to talk on ⌃⌥Space (hold
to speak; the finalized text is pasted into the front application when
Accessibility access is granted, else copied). **Control + Shift + R** starts
and stops a normal recording from any application.

It captures no audio itself. The helper launches the capture backend; this
package depends on `clients/macos_capture` only for the floating caption
panel it displays.

Build and test:

```sh
./scripts/build-macos-menubar
swift test --package-path voxink_menubar
```

The development bundle lands in `voxink_menubar/.build/VoxInkMenuBarDev.app`,
which is where `voxink-dev` looks for it.

## Floating caption panel

`FloatingCaptionPanelController` is the AppKit/SwiftUI overlay the menu-bar app
shows. It is an always-on-top, frosted dark panel that can be
made click-through, dragged by its header, and resized from its edges; its
frame and text size are remembered between launches, and on first launch it
sits bottom-centre above the Dock.

The header shows the session state (Starting, Live with elapsed time, Paused,
Finishing, Ready) and a small level bar per source fed through `setLevels`
(the menu-bar app forwards the `levels` field of each status poll). Hovering
reveals a toolbar: mark this moment (shown when the host sets `onMark`, which
the menu-bar app routes to the mark endpoint), smaller and larger text, copy
the visible transcript, and hide the panel (the host's `onHide` callback
keeps its own toggle in sync). The body is a scrolling stream of the newest 80
entries grouped into turns: a turn begins whenever the source changes
(microphone, system audio, or a named application) or a mark arrives, and
carries one small label with the time of its first caption. When the service
recognizes a remembered voice live, the caption's `speaker` becomes the turn
label and a change of name starts a new turn. The experimental live diarizer
sends the same field with `speaker_provisional`, and `SpeakerLabel.compose`
turns it into "Speaker 1 · provisional" (or "… · overlap · provisional", or
"Unassigned · provisional" for speech it could not attribute) so a session-local
guess never reads like a remembered name. Long sentences
wrap in full; partial captions are dimmed and replaced in place by stable
event ID; translations sit under their caption; marks are amber rows. The
stream follows new captions until you scroll up, when a "Latest" chip
appears to jump back down. After five seconds without a caption or mark
(`idleCollapseDelay`) the panel shrinks into a small bubble at its bottom
centre that shows only the status dot; hovering or clicking the bubble, or
the next caption, brings the panel back in the same place. It stays open
while the pointer is over it or while you have scrolled back. The app feeds it through `show(_:)`, `showMark`,
`attachTranslation`, and `setStatus`; `show` never presents the window, so
the app calls `showWindow` when it wants it on screen.
