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
