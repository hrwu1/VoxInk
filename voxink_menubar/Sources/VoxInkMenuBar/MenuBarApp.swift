import AppKit
import Carbon.HIToolbox
import Foundation
import VoxInkCapture

// The menu-bar application is a control surface over the same local API the
// browser uses. It never captures audio itself: it asks the helper to start
// the native capture process, polls the caption feed, shows the floating
// panel, and offers a push-to-talk hotkey that pastes what was said into the
// front application. Launched by `voxink-app`, which supplies the
// helper's address and credential in the environment.

private struct Environment {
    let baseURL: URL
    let token: String
    let outputRoot: String?

    static func load() -> Environment? {
        let values = ProcessInfo.processInfo.environment
        guard let token = values["VOXINK_AUTH_SECRET"], !token.isEmpty,
              let raw = values["VOXINK_SERVICE_URL"], let url = URL(string: raw),
              let host = url.host?.lowercased(), ["127.0.0.1", "localhost", "::1"].contains(host)
        else { return nil }
        return Environment(baseURL: url, token: token, outputRoot: values["VOXINK_OUTPUT_ROOT"])
    }
}

private struct FeedEvent: Decodable {
    let type: String?
    let segmentID: String
    let source: String?
    let text: String
    let startMs: Int64
    let endMs: Int64
    let isFinal: Bool?
    let channelName: String?
    let language: String?
    let speaker: String?
    let speakerProvisional: Bool?
    let speakerOverlap: Bool?

    enum CodingKeys: String, CodingKey {
        case type, source, text, language, speaker
        case segmentID = "segment_id"
        case startMs = "start_ms"
        case endMs = "end_ms"
        case isFinal = "is_final"
        case channelName = "channel_name"
        case speakerProvisional = "speaker_provisional"
        case speakerOverlap = "speaker_overlap"
    }
}

/// Availability and running state of the experimental live diarizer, as the
/// helper reports it to every client.
private struct LiveDiarizationView: Decodable {
    var available = false
    var requested = false
    var state = "disabled"
    var error: String?
}

private struct CapabilitiesView: Decodable {
    let liveDiarization: LiveDiarizationView?

    enum CodingKeys: String, CodingKey {
        case liveDiarization = "live_diarization"
    }
}

private struct StatusView: Decodable {
    let state: String
    let sessionID: String?
    let events: [FeedEvent]
    let nextCursor: Int
    let error: String?
    /// The helper's latest status line (`code: message`) while it runs.
    let notice: String?
    let levels: [String: Double]?
    let liveDiarization: LiveDiarizationView?

    enum CodingKeys: String, CodingKey {
        case state, events, error, notice, levels
        case sessionID = "session_id"
        case nextCursor = "next_cursor"
        case liveDiarization = "live_diarization"
    }
}

/// Minimal JSON client for the control API.
private final class ControlAPI {
    private let environment: Environment
    private let session = URLSession(configuration: .ephemeral)

    init(_ environment: Environment) { self.environment = environment }

    func status(after cursor: Int) async throws -> StatusView {
        try await request("GET", "/v1/ui/capture/status?after=\(cursor)")
    }

    func capabilities() async throws -> CapabilitiesView {
        try await request("GET", "/v1/ui/capture/capabilities")
    }

    func start(
        sources: [String], language: String?, retainAudio: Bool = false, diarize: Bool = false,
        liveDiarization: Bool = false
    ) async throws -> StatusView {
        var body: [String: Any] = [
            "sources": sources, "retain_audio": retainAudio, "diarize": diarize,
            "live_diarization": liveDiarization,
        ]
        if let language { body["language"] = language }
        return try await request("POST", "/v1/ui/capture/start", body: body)
    }

    func control(_ action: String) async throws -> StatusView {
        try await request("POST", "/v1/ui/capture/\(action)")
    }

    func mark() async throws {
        let _: EmptyResponse = try await request("POST", "/v1/ui/capture/mark", body: ["label": ""])
    }

    func reveal() async throws {
        let _: EmptyResponse = try await request("POST", "/v1/ui/actions/reveal", body: [:])
    }

    private struct EmptyResponse: Decodable {}

    private func request<T: Decodable>(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> T {
        var request = URLRequest(url: environment.baseURL.appendingPathComponent(path).absoluteURL)
        // appendingPathComponent escapes the query; rebuild from the string instead.
        request.url = URL(string: environment.baseURL.absoluteString + path)
        request.httpMethod = method
        request.setValue("Bearer \(environment.token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
            throw MenuBarError(String(describing: detail ?? "request failed (\(http.statusCode))"))
        }
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

private struct MenuBarError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

@MainActor
private final class MenuBarController: NSObject, NSMenuDelegate {
    private let api: ControlAPI
    private let environment: Environment
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let captionPanel = FloatingCaptionPanelController()
    private var showCaptions = true
    /// Session options for Start, persisted like the browser's checkboxes and
    /// on until turned off. Push to talk is dictation and never keeps audio.
    private let defaults: UserDefaults = {
        let defaults = UserDefaults.standard
        defaults.register(defaults: ["keepAudio": true, "identifySpeakers": true, "liveSpeakers": false])
        return defaults
    }()
    private var keepAudio: Bool {
        get { defaults.bool(forKey: "keepAudio") }
        set { defaults.set(newValue, forKey: "keepAudio") }
    }
    private var identifySpeakers: Bool {
        get { defaults.bool(forKey: "identifySpeakers") }
        set { defaults.set(newValue, forKey: "identifySpeakers") }
    }
    /// Experimental, so off until asked for, like the browser's checkbox.
    private var liveSpeakers: Bool {
        get { defaults.bool(forKey: "liveSpeakers") }
        set { defaults.set(newValue, forKey: "liveSpeakers") }
    }
    private var liveDiarization = LiveDiarizationView()
    /// A degraded-recording line from the helper, shown until it clears.
    private var helperNotice: String?
    private var readingCapabilities = false
    private var pollTimer: Timer?
    private var cursor = 0
    private var state = "ready"
    private var lastError: String?
    private var startedAt: Date?
    /// Finals seen since push-to-talk began, in order.
    private var dictated: [FeedEvent] = []
    private var pushToTalkArmed = false
    private var pushToTalkActive = false
    private var pushToTalkSince: Int64 = 0
    private lazy var recordingShortcut = RecordingShortcut(defaults: defaults)
    private var commandInFlight = false
    private var commandRevision = 0
    private var hotKeyRefs: [EventHotKeyRef] = []
    private var hotKeyHandler: EventHandlerRef?
    private var hotKeyErrors: [String] = []

    init(api: ControlAPI, environment: Environment) {
        self.api = api
        self.environment = environment
        super.init()
        statusItem.button?.title = "◉"
        statusItem.button?.toolTip = "VoxInk"
        menu.delegate = self
        // Every item's enabled state is decided in `rebuildMenu` from the
        // session state; AppKit's automatic enabling would override all of it
        // and offer Stop with nothing recording, or a session option mid-session.
        menu.autoenablesItems = false
        statusItem.menu = menu
        captionPanel.onHide = { [weak self] in self?.showCaptions = false }
        captionPanel.onMark = { [weak self] in self?.markMoment() }
        rebuildMenu()
        startPolling()
        installHotKey()
        rebuildMenu()
        Task { await refreshCapabilities() }
    }

    // MARK: Menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        // The status poll is idle between sessions, so an experiment runtime
        // that appeared since launch is picked up when the menu is opened.
        if !liveDiarization.available { Task { await refreshCapabilities() } }
        rebuildMenu()
    }

    /// Availability is a property of the helper, not of a session, so it comes
    /// from the capabilities endpoint the browser reads for the same checkbox.
    private func refreshCapabilities() async {
        guard !readingCapabilities else { return }
        readingCapabilities = true
        defer { readingCapabilities = false }
        guard let view = try? await api.capabilities().liveDiarization else { return }
        // Only availability is read here. The helper keeps reporting the last
        // session's live state, which may belong to the browser rather than to
        // this app, so session state comes from a session this app started.
        liveDiarization.available = view.available
        if !liveDiarization.requested { liveDiarization.error = view.error }
        rebuildMenu()
    }

    private func rebuildMenu() {
        menu.removeAllItems()
        let title = NSMenuItem(title: statusLine(), action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)
        for error in hotKeyErrors {
            let item = NSMenuItem(title: error, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        if let lastError {
            let item = NSMenuItem(title: lastError, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        if let line = liveSpeakerStatusLine() {
            let item = NSMenuItem(title: line, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        if let helperNotice {
            let item = NSMenuItem(title: helperNotice, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let idle = (state == "ready" || state == "failed") && !commandInFlight
        let canStop = (state == "recording" || state == "paused") && !commandInFlight
        let recordingTitle: String
        switch state {
        case "starting": recordingTitle = "Starting…"
        case "stopping": recordingTitle = "Finalizing…"
        case "recording", "paused": recordingTitle = "Stop Recording"
        default: recordingTitle = "Start Recording"
        }
        let recording = NSMenuItem(
            title: recordingTitle + (pushToTalkArmed ? "" : " (⌃⇧R)"),
            action: canStop ? #selector(stopCapture) : #selector(startCapture), keyEquivalent: ""
        )
        recording.target = self
        recording.isEnabled = canStop || (idle && !pushToTalkArmed)
        menu.addItem(recording)
        let selectedSources = recordingShortcut.sources
        let sourceLabel = RecordingShortcut.sourceOptions.first { $0.sources == selectedSources }!.title
        let source = NSMenuItem(title: "Audio Source: " + sourceLabel, action: nil, keyEquivalent: "")
        source.isEnabled = idle && !pushToTalkArmed
        let sourceMenu = NSMenu()
        sourceMenu.autoenablesItems = false
        for (label, sources) in RecordingShortcut.sourceOptions {
            let item = NSMenuItem(title: label, action: #selector(selectSource(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = sources
            item.state = sources == selectedSources ? .on : .off
            item.isEnabled = source.isEnabled
            sourceMenu.addItem(item)
        }
        source.submenu = sourceMenu
        menu.addItem(source)
        let audio = NSMenuItem(title: "Keep audio", action: #selector(toggleKeepAudio), keyEquivalent: "")
        audio.target = self
        audio.state = keepAudio ? .on : .off
        audio.isEnabled = idle
        menu.addItem(audio)
        let live = NSMenuItem(
            title: "Live speakers (experimental)", action: #selector(toggleLiveSpeakers), keyEquivalent: ""
        )
        live.target = self
        live.state = liveDiarization.available && liveSpeakers ? .on : .off
        live.isEnabled = idle && liveDiarization.available
        if liveDiarization.available {
            live.toolTip = "Label microphone speech while recording. Labels are provisional and are not written to the transcript."
        } else {
            // A disabled item explains itself, the way the browser's checkbox
            // carries the helper's reason under it.
            live.attributedTitle = Self.twoLineTitle(
                live.title,
                liveDiarization.error ?? "The Diart experiment runtime is unavailable on this helper"
            )
        }
        menu.addItem(live)
        let speakers = NSMenuItem(
            title: keepAudio ? "Identify speakers after Stop" : "Identify speakers after Stop (needs kept audio)",
            action: #selector(toggleIdentifySpeakers), keyEquivalent: ""
        )
        speakers.target = self
        speakers.state = keepAudio && identifySpeakers ? .on : .off
        speakers.isEnabled = idle && keepAudio
        menu.addItem(speakers)
        menu.addItem(.separator())
        let pause = NSMenuItem(title: state == "paused" ? "Resume" : "Pause", action: #selector(togglePause), keyEquivalent: "")
        pause.target = self
        pause.isEnabled = (state == "recording" || state == "paused") && !pushToTalkArmed && !commandInFlight
        menu.addItem(pause)
        let mark = NSMenuItem(title: "Mark this moment", action: #selector(markMoment), keyEquivalent: "m")
        mark.target = self
        mark.isEnabled = state == "recording" || state == "paused"
        menu.addItem(mark)
        menu.addItem(.separator())
        let ptt = NSMenuItem(
            title: pushToTalkArmed ? "Push to talk: hold ⌃⌥Space (on)" : "Enable push to talk (⌃⌥Space)",
            action: #selector(togglePushToTalk), keyEquivalent: ""
        )
        ptt.target = self
        ptt.state = pushToTalkArmed ? .on : .off
        ptt.isEnabled = (pushToTalkArmed || idle) && !commandInFlight
        menu.addItem(ptt)
        let captions = NSMenuItem(title: "Show floating captions", action: #selector(toggleCaptions), keyEquivalent: "")
        captions.target = self
        captions.state = showCaptions ? .on : .off
        menu.addItem(captions)
        let clickThrough = NSMenuItem(title: "Click through captions", action: #selector(toggleClickThrough), keyEquivalent: "")
        clickThrough.target = self
        clickThrough.state = clickThroughEnabled ? .on : .off
        menu.addItem(clickThrough)
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open transcript folder", action: #selector(openFolder), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        let quit = NSMenuItem(title: "Quit VoxInk", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    private var clickThroughEnabled = false

    /// A menu item with its reason underneath, in the smaller secondary style
    /// AppKit uses for such hints.
    private static func twoLineTitle(_ title: String, _ detail: String) -> NSAttributedString {
        let text = NSMutableAttributedString(
            string: title,
            attributes: [.font: NSFont.menuFont(ofSize: 0)]
        )
        text.append(NSAttributedString(
            string: "\n" + detail,
            attributes: [
                .font: NSFont.menuFont(ofSize: NSFont.smallSystemFontSize),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
        ))
        return text
    }

    private func statusLine() -> String {
        switch state {
        case "recording":
            let elapsed = Int(Date().timeIntervalSince(startedAt ?? Date()))
            return String(format: "VoxInk — Recording %02d:%02d", elapsed / 60, elapsed % 60)
        case "paused": return "VoxInk — Paused"
        case "starting": return "VoxInk — Starting…"
        case "stopping": return "VoxInk — Finalizing…"
        case "failed": return "VoxInk — Needs attention"
        default: return pushToTalkArmed ? "VoxInk — Push to talk ready" : "VoxInk — Ready"
        }
    }

    /// What the browser shows as a chip beside the captions, as one line.
    private func liveSpeakerStatusLine() -> String? {
        guard liveDiarization.requested, liveDiarization.state != "disabled" else { return nil }
        switch liveDiarization.state {
        case "starting": return "Live speakers: loading the model…"
        case "failed": return "Live speakers: " + (liveDiarization.error ?? "failed")
        case "stopped": return "Live speakers: labels were provisional"
        default: return "Live speakers: on — labels are provisional"
        }
    }

    private func refreshStatusButton() {
        statusItem.button?.title = state == "recording" ? "●" : state == "paused" ? "Ⅱ" : "◉"
    }

    // MARK: Actions

    @objc private func selectSource(_ sender: NSMenuItem) {
        guard RecordingShortcut.action(state: state, pushToTalkArmed: pushToTalkArmed, busy: commandInFlight) == .start else { return }
        guard let sources = sender.representedObject as? [String] else { return }
        recordingShortcut.remember(sources: sources)
    }

    @objc private func startCapture() {
        Task { await begin(sources: recordingShortcut.sources) }
    }

    private func begin(sources: [String]) async {
        guard RecordingShortcut.action(state: state, pushToTalkArmed: pushToTalkArmed, busy: commandInFlight) == .start else { return }
        commandInFlight = true
        commandRevision += 1
        defer { commandInFlight = false }
        state = "starting"
        lastError = nil
        helperNotice = nil
        captionPanel.setNotice(nil)
        refreshStatusButton()
        captionPanel.clear()
        syncPanelStatus()
        if showCaptions { captionPanel.showWindow() }
        // Live diarization reads the microphone stream, so it is only asked
        // for when one is being captured; the helper refuses it otherwise.
        let wantsLiveSpeakers = liveSpeakers && liveDiarization.available && sources.contains("microphone")
        liveDiarization.requested = wantsLiveSpeakers
        liveDiarization.state = wantsLiveSpeakers ? "starting" : "disabled"
        liveDiarization.error = nil
        do {
            let view = try await api.start(
                sources: sources, language: nil, retainAudio: keepAudio,
                diarize: keepAudio && identifySpeakers && sources.contains("microphone"),
                liveDiarization: wantsLiveSpeakers
            )
            cursor = view.nextCursor
            startedAt = Date()
            state = view.state
            if let live = view.liveDiarization { liveDiarization = live }
        } catch {
            state = "failed"
            lastError = error.localizedDescription
            if wantsLiveSpeakers {
                liveDiarization.state = "failed"
                liveDiarization.error = error.localizedDescription
            }
        }
        syncPanelStatus()
        refreshStatusButton()
    }

    /// Mirror the session state into the caption panel's header.
    private func syncPanelStatus() {
        switch state {
        case "recording": captionPanel.setStatus(.live(since: startedAt ?? Date()))
        case "paused": captionPanel.setStatus(.paused)
        case "starting": captionPanel.setStatus(.starting)
        case "stopping": captionPanel.setStatus(.finishing)
        default: captionPanel.setStatus(.idle)
        }
    }

    @objc private func toggleKeepAudio() { keepAudio.toggle() }

    @objc private func toggleIdentifySpeakers() { identifySpeakers.toggle() }

    @objc private func toggleLiveSpeakers() { liveSpeakers.toggle() }

    @objc private func togglePause() {
        Task { await control(state == "paused" ? "resume" : "pause") }
    }

    @objc private func stopCapture() {
        Task { await control("stop") }
    }

    private func control(_ action: String) async {
        guard !commandInFlight else { return }
        commandInFlight = true
        commandRevision += 1
        defer { commandInFlight = false }
        let previousState = state
        if action == "stop" {
            state = "stopping"
            syncPanelStatus()
            refreshStatusButton()
        }
        do {
            let view = try await api.control(action)
            apply(view)
        } catch {
            state = previousState
            lastError = error.localizedDescription
            syncPanelStatus()
        }
        refreshStatusButton()
    }

    @objc private func markMoment() {
        Task {
            do { try await api.mark() } catch { lastError = error.localizedDescription }
        }
    }

    @objc private func toggleCaptions() {
        showCaptions.toggle()
        if showCaptions { captionPanel.showWindow() } else { captionPanel.hideWindow() }
    }

    @objc private func toggleClickThrough() {
        clickThroughEnabled.toggle()
        captionPanel.setClickThrough(clickThroughEnabled)
    }

    @objc private func openFolder() {
        Task {
            do { try await api.reveal() } catch {
                if let root = environment.outputRoot {
                    NSWorkspace.shared.open(URL(fileURLWithPath: root))
                } else {
                    lastError = error.localizedDescription
                }
            }
        }
    }

    @objc private func quit() {
        Task {
            if state == "recording" || state == "paused" { await control("stop") }
            NSApplication.shared.terminate(nil)
        }
    }

    // MARK: Polling

    private func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.poll() }
        }
    }

    private func poll() async {
        guard !commandInFlight, state != "ready" || pushToTalkArmed else { return }
        let revision = commandRevision
        do {
            let view = try await api.status(after: cursor)
            guard !commandInFlight, revision == commandRevision else { return }
            apply(view)
        } catch {
            // A helper that is restarting answers again on the next tick.
        }
    }

    private func apply(_ view: StatusView) {
        cursor = view.nextCursor
        if let live = view.liveDiarization { liveDiarization = live }
        captionPanel.setLevels(CaptionLevels(microphone: view.levels?["microphone"], system: view.levels?["system"]))
        let notice = HelperNotice.line(from: view.notice)
        if notice != helperNotice {
            helperNotice = notice
            captionPanel.setNotice(notice)
        }
        if view.state != state {
            state = view.state
            if state == "ready" || state == "failed" { startedAt = nil }
            lastError = view.error
            syncPanelStatus()
            refreshStatusButton()
        }
        for event in view.events {
            let kind = event.type ?? "transcript"
            if kind == "translation" {
                captionPanel.attachTranslation(event.text, to: event.segmentID)
                continue
            }
            if kind == "mark" {
                captionPanel.showMark(atMilliseconds: event.startMs, label: event.text)
                continue
            }
            if pushToTalkActive || (pushToTalkArmed && event.startMs >= pushToTalkSince), event.isFinal ?? true {
                if event.startMs >= pushToTalkSince { dictated.append(event) }
            }
            // The panel keeps its history while hidden, so showing it again
            // catches up instead of starting blank.
            captionPanel.show(CaptionItem(
                id: event.segmentID,
                source: event.source == "system" ? .system : .microphone,
                text: event.text,
                startMilliseconds: event.startMs,
                endMilliseconds: event.endMs,
                isFinal: event.isFinal ?? true,
                channelName: event.channelName,
                speakerName: SpeakerLabel.compose(
                    speaker: event.speaker, provisional: event.speakerProvisional ?? false,
                    overlap: event.speakerOverlap ?? false
                )
            ))
        }
    }

    // MARK: Push to talk

    @objc private func togglePushToTalk() {
        Task { await pushToTalkArmed ? disarmPushToTalk() : armPushToTalk() }
    }

    /// Start a microphone session and hold it paused; the key resumes and
    /// pauses it, so each press costs no helper start-up.
    private func armPushToTalk() async {
        guard !commandInFlight, state == "ready" || state == "failed" else { return }
        commandInFlight = true
        commandRevision += 1
        defer { commandInFlight = false }
        state = "starting"
        refreshStatusButton()
        do {
            let view = try await api.start(sources: ["microphone"], language: nil)
            cursor = view.nextCursor
            // Give the helper a moment to open its stream before pausing it.
            try await Task.sleep(nanoseconds: 1_500_000_000)
            let paused = try await api.control("pause")
            state = paused.state
            pushToTalkArmed = true
            captionPanel.hideWindow()
        } catch {
            state = "failed"
            lastError = error.localizedDescription
        }
        refreshStatusButton()
    }

    private func disarmPushToTalk() async {
        pushToTalkArmed = false
        pushToTalkActive = false
        await control("stop")
        if showCaptions { captionPanel.showWindow() }
    }

    fileprivate func hotKeyPressed() {
        guard pushToTalkArmed, !pushToTalkActive else { return }
        pushToTalkActive = true
        dictated.removeAll()
        pushToTalkSince = Int64(Date().timeIntervalSince(startedAt ?? Date()) * 1000)
        statusItem.button?.title = "🎙"
        Task {
            do {
                let view = try await api.control("resume")
                startedAt = startedAt ?? Date()
                state = view.state
                // Everything from now on belongs to this press.
                pushToTalkSince = 0
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    fileprivate func hotKeyReleased() {
        guard pushToTalkArmed, pushToTalkActive else { return }
        Task {
            do {
                let view = try await api.control("pause")
                state = view.state
                // Pausing flushes the last utterance; collect it, then paste.
                for _ in 0..<12 {
                    try await Task.sleep(nanoseconds: 250_000_000)
                    apply(try await api.status(after: cursor))
                    if !dictated.isEmpty { break }
                }
                pushToTalkActive = false
                refreshStatusButton()
                paste(dictated.map(\.text).joined(separator: " "))
            } catch {
                pushToTalkActive = false
                lastError = error.localizedDescription
                refreshStatusButton()
            }
        }
    }

    /// Put the text on the pasteboard and press ⌘V in the front app when
    /// Accessibility allows it; otherwise it waits on the clipboard.
    private func paste(_ text: String) {
        let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(cleaned, forType: .string)
        guard AXIsProcessTrusted() else {
            lastError = "Copied. Grant Accessibility access to paste automatically."
            return
        }
        let source = CGEventSource(stateID: .combinedSessionState)
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_ANSI_V), keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_ANSI_V), keyDown: false)
        keyDown?.flags = .maskCommand
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cghidEventTap)
        keyUp?.post(tap: .cghidEventTap)
    }

    // MARK: Global shortcuts

    fileprivate func recordingKeyPressed() {
        guard let action = recordingShortcut.press(
            state: state, pushToTalkArmed: pushToTalkArmed, busy: commandInFlight
        ) else { return }
        Task {
            // Recheck after scheduling: a menu action may have run meanwhile.
            guard RecordingShortcut.action(
                state: state, pushToTalkArmed: pushToTalkArmed, busy: commandInFlight
            ) == action else { return }
            switch action {
            case .start: await begin(sources: recordingShortcut.sources)
            case .stop: await control("stop")
            }
        }
    }

    private func installHotKey() {
        var eventTypes = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
        ]
        let controller = Unmanaged.passUnretained(self).toOpaque()
        let handlerStatus = InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            guard let userData, let event else { return noErr }
            var hotKeyID = EventHotKeyID()
            guard GetEventParameter(
                event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID
            ) == noErr,
            let key = MenuBarHotKey(signature: hotKeyID.signature, id: hotKeyID.id)
            else { return OSStatus(eventNotHandledErr) }
            let controller = Unmanaged<MenuBarController>.fromOpaque(userData).takeUnretainedValue()
            let kind = GetEventKind(event)
            Task { @MainActor in
                switch (key, kind) {
                case (.pushToTalk, UInt32(kEventHotKeyPressed)): controller.hotKeyPressed()
                case (.pushToTalk, UInt32(kEventHotKeyReleased)): controller.hotKeyReleased()
                case (.recording, UInt32(kEventHotKeyPressed)): controller.recordingKeyPressed()
                case (.recording, UInt32(kEventHotKeyReleased)): controller.recordingShortcut.release()
                default: break
                }
            }
            return noErr
        }, eventTypes.count, &eventTypes, controller, &hotKeyHandler)
        guard handlerStatus == noErr else {
            hotKeyErrors.append("Global shortcuts unavailable (error \(handlerStatus)). Restart VoxInk to retry.")
            return
        }
        for (key, code, modifiers, label) in [
            (MenuBarHotKey.pushToTalk, kVK_Space, controlKey | optionKey, "⌃⌥Space"),
            (MenuBarHotKey.recording, kVK_ANSI_R, controlKey | shiftKey, "⌃⇧R"),
        ] {
            var reference: EventHotKeyRef?
            let status = RegisterEventHotKey(
                UInt32(code), UInt32(modifiers),
                EventHotKeyID(signature: MenuBarHotKey.signature, id: key.rawValue),
                GetApplicationEventTarget(), 0, &reference
            )
            if status == noErr, let reference {
                hotKeyRefs.append(reference)
            } else {
                hotKeyErrors.append("\(label) unavailable (error \(status)). Check other apps' shortcuts, then restart VoxInk.")
            }
        }
    }
}

@main
private struct VoxInkMenuBarApp {
    static func main() {
        guard let environment = Environment.load() else {
            FileHandle.standardError.write(Data(
                "voxink-menubar must be launched by voxink-app (VOXINK_SERVICE_URL and VOXINK_AUTH_SECRET are required).\n".utf8
            ))
            exit(2)
        }
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let controller = MenuBarController(api: ControlAPI(environment), environment: environment)
        withExtendedLifetime(controller) {
            application.run()
        }
    }
}
