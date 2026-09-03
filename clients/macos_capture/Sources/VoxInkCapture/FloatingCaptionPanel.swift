import AppKit
import SwiftUI

public struct CaptionItem: Identifiable, Equatable, Sendable {
    public let id: String
    public let source: AudioSource
    public let text: String
    public let startMilliseconds: Int64
    public let endMilliseconds: Int64?
    public let isFinal: Bool
    /// Application name for a per-application system stream, if any.
    public let channelName: String?
    /// On-device translation shown as a second line, if any.
    public let translation: String?

    public init(
        id: String,
        source: AudioSource,
        text: String,
        startMilliseconds: Int64,
        endMilliseconds: Int64? = nil,
        isFinal: Bool,
        channelName: String? = nil,
        translation: String? = nil
    ) {
        self.id = id
        self.source = source
        self.text = text
        self.startMilliseconds = startMilliseconds
        self.endMilliseconds = endMilliseconds
        self.isFinal = isFinal
        self.channelName = channelName
        self.translation = translation
    }

    public func withTranslation(_ translation: String?) -> CaptionItem {
        CaptionItem(
            id: id, source: source, text: text, startMilliseconds: startMilliseconds,
            endMilliseconds: endMilliseconds, isFinal: isFinal, channelName: channelName,
            translation: translation
        )
    }
}

/// What the host is doing, shown in the panel's header.
public enum CaptionPanelStatus: Equatable, Sendable {
    case idle
    case starting
    case live(since: Date)
    case paused
    case finishing
}

/// Always-on-top caption overlay. A host feeds normalized partial/final
/// transcript events through `show(_:)`, marks through `showMark`, and its
/// session state through `setStatus`; the panel keeps a bounded, scrollable
/// history grouped into speaker turns and remembers its frame and text size.
@MainActor
public final class FloatingCaptionPanelController {
    private static let frameName = "VoxInkFloatingCaptions"
    private let model = CaptionPanelModel()
    private let panel: NSPanel
    private var scrollMonitor: Any?

    /// Called when the person hides the panel from its own toolbar, so a
    /// host's "show captions" toggle can follow.
    public var onHide: (() -> Void)?

    public init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 220),
            styleMask: [.borderless, .nonactivatingPanel, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.isMovableByWindowBackground = true
        panel.minSize = NSSize(width: 340, height: 140)
        panel.animationBehavior = .utilityWindow
        // Captions sit over arbitrary content; a pinned dark look keeps them legible.
        panel.appearance = NSAppearance(named: .darkAqua)
        model.onHide = { [weak self] in self?.hideFromPanel() }
        panel.contentView = NSHostingView(rootView: FloatingCaptionView(model: model))
        let restored = panel.setFrameUsingName(Self.frameName)
        panel.setFrameAutosaveName(Self.frameName)
        if !restored { placeAboveDock() }
        // Scrolling up stops the stream from following new captions; the
        // view turns following back on when the bottom comes into view.
        scrollMonitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
            if let self, event.window === self.panel, event.scrollingDeltaY > 0 {
                self.model.following = false
            }
            return event
        }
    }

    public func show(_ item: CaptionItem) {
        model.upsert(item)
    }

    /// Attach a translation that arrived after its caption was shown.
    public func attachTranslation(_ translation: String, to id: String) {
        model.attachTranslation(translation, to: id)
    }

    /// Add a timestamped marker row to the stream.
    public func showMark(atMilliseconds milliseconds: Int64, label: String = "") {
        model.addMark(atMilliseconds: milliseconds, label: label)
    }

    public func setStatus(_ status: CaptionPanelStatus) {
        model.status = status
    }

    public func showWindow() {
        panel.orderFrontRegardless()
    }

    public func hideWindow() {
        panel.orderOut(nil)
    }

    public func setClickThrough(_ enabled: Bool) {
        panel.ignoresMouseEvents = enabled
    }

    public func clear() {
        model.clear()
    }

    private func hideFromPanel() {
        hideWindow()
        onHide?()
    }

    private func placeAboveDock() {
        guard let screen = NSScreen.main else { panel.center(); return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: visible.midX - size.width / 2, y: visible.minY + 48))
    }
}

// MARK: - Model

enum CaptionEntry: Identifiable, Equatable {
    case caption(CaptionItem)
    case mark(id: String, milliseconds: Int64, label: String)

    var id: String {
        switch self {
        case .caption(let item): return item.id
        case .mark(let id, _, _): return id
        }
    }

    var startMilliseconds: Int64 {
        switch self {
        case .caption(let item): return item.startMilliseconds
        case .mark(_, let milliseconds, _): return milliseconds
        }
    }
}

/// Consecutive captions from one source, or a single mark.
struct CaptionTurn: Identifiable, Equatable {
    enum Kind: Equatable {
        case speech(source: AudioSource, channelName: String?)
        case mark(label: String)
    }

    let id: String
    let kind: Kind
    let startMilliseconds: Int64
    var captions: [CaptionItem]

    var label: String {
        switch kind {
        case .speech(.microphone, _): return "Microphone"
        case .speech(.system, let channelName): return channelName ?? "System audio"
        case .mark(let label): return label.isEmpty ? "Marked" : label
        }
    }

    static func group(_ entries: [CaptionEntry]) -> [CaptionTurn] {
        var turns = [CaptionTurn]()
        for entry in entries {
            switch entry {
            case .mark(let id, let milliseconds, let label):
                turns.append(CaptionTurn(id: id, kind: .mark(label: label), startMilliseconds: milliseconds, captions: []))
            case .caption(let item):
                let kind = Kind.speech(source: item.source, channelName: item.channelName)
                if let last = turns.indices.last, turns[last].kind == kind {
                    turns[last].captions.append(item)
                } else {
                    turns.append(CaptionTurn(id: item.id, kind: kind, startMilliseconds: item.startMilliseconds, captions: [item]))
                }
            }
        }
        return turns
    }
}

@MainActor
final class CaptionPanelModel: ObservableObject {
    static let historyLimit = 80

    @Published private(set) var entries = [CaptionEntry]()
    @Published var status: CaptionPanelStatus = .idle
    /// Whether the stream scrolls to each new caption.
    @Published var following = true
    var onHide: (() -> Void)?

    var turns: [CaptionTurn] { CaptionTurn.group(entries) }

    func upsert(_ item: CaptionItem) {
        if let index = entries.firstIndex(where: { $0.id == item.id }), case .caption(let existing) = entries[index] {
            // A final that replaces a partial keeps a translation that already
            // arrived for the same segment.
            entries[index] = .caption(
                item.translation == nil && existing.translation != nil ? item.withTranslation(existing.translation) : item
            )
        } else {
            entries.append(.caption(item))
        }
        settle()
    }

    func attachTranslation(_ translation: String, to id: String) {
        guard let index = entries.firstIndex(where: { $0.id == id }), case .caption(let item) = entries[index] else { return }
        entries[index] = .caption(item.withTranslation(translation))
    }

    func addMark(atMilliseconds milliseconds: Int64, label: String) {
        entries.append(.mark(id: "mark-\(UUID().uuidString)", milliseconds: milliseconds, label: label))
        settle()
    }

    func clear() {
        entries.removeAll()
        following = true
    }

    /// Everything on screen as plain text, one line per caption.
    func transcriptText() -> String {
        turns.map { turn in
            switch turn.kind {
            case .mark:
                return "[\(Self.clock(turn.startMilliseconds))] ◆ \(turn.label)"
            case .speech:
                return turn.captions.map { item in
                    var line = "[\(Self.clock(item.startMilliseconds))] \(turn.label): \(item.text)"
                    if let translation = item.translation { line += "\n    \(translation)" }
                    return line
                }.joined(separator: "\n")
            }
        }.joined(separator: "\n")
    }

    static func clock(_ milliseconds: Int64) -> String {
        let seconds = max(0, milliseconds) / 1000
        if seconds >= 3600 {
            return String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
        }
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }

    private func settle() {
        entries.sort {
            if $0.startMilliseconds == $1.startMilliseconds { return $0.id < $1.id }
            return $0.startMilliseconds < $1.startMilliseconds
        }
        if entries.count > Self.historyLimit { entries.removeFirst(entries.count - Self.historyLimit) }
    }
}

// MARK: - Views

private enum Palette {
    static let microphone = Color(red: 0.98, green: 0.47, blue: 0.27)
    static let system = Color(red: 0.42, green: 0.72, blue: 0.96)
    static let mark = Color(red: 0.99, green: 0.76, blue: 0.33)
    static let corner: CGFloat = 16
    static let minimumFontSize = 13.0
    static let maximumFontSize = 28.0
}

private struct FloatingCaptionView: View {
    @ObservedObject var model: CaptionPanelModel
    @AppStorage("captionFontSize") private var fontSize = 17.0
    @State private var hovering = false

    var body: some View {
        VStack(spacing: 0) {
            header
            CaptionStream(model: model, fontSize: fontSize)
        }
        .background(ZStack { HUDBackground(); Color.black.opacity(0.28) })
        .overlay(
            RoundedRectangle(cornerRadius: Palette.corner, style: .continuous)
                .strokeBorder(.white.opacity(0.14), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: Palette.corner, style: .continuous))
        .onHover { hovering = $0 }
    }

    private var header: some View {
        HStack(spacing: 10) {
            StatusBadge(status: model.status)
            Spacer(minLength: 0)
            HStack(spacing: 4) {
                ToolbarButton(symbol: "textformat.size.smaller", help: "Smaller text") {
                    fontSize = max(Palette.minimumFontSize, fontSize - 2)
                }
                .disabled(fontSize <= Palette.minimumFontSize)
                ToolbarButton(symbol: "textformat.size.larger", help: "Larger text") {
                    fontSize = min(Palette.maximumFontSize, fontSize + 2)
                }
                .disabled(fontSize >= Palette.maximumFontSize)
                ToolbarButton(symbol: "doc.on.doc", help: "Copy transcript") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(model.transcriptText(), forType: .string)
                }
                .disabled(model.entries.isEmpty)
                ToolbarButton(symbol: "xmark", help: "Hide captions") { model.onHide?() }
            }
            .opacity(hovering ? 1 : 0)
            .animation(.easeOut(duration: 0.15), value: hovering)
        }
        .padding(.leading, 14)
        .padding(.trailing, 10)
        .padding(.top, 9)
        .padding(.bottom, 5)
    }
}

private struct StatusBadge: View {
    let status: CaptionPanelStatus

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            if case .live(let since) = status {
                TimelineView(.periodic(from: since, by: 1)) { context in
                    Text("Live · \(CaptionPanelModel.clock(Int64(context.date.timeIntervalSince(since) * 1000)))")
                }
            } else {
                Text(label)
            }
        }
        .font(.system(size: 11, weight: .semibold).monospacedDigit())
        .foregroundStyle(.white.opacity(0.72))
        .accessibilityElement(children: .combine)
    }

    private var label: String {
        switch status {
        case .idle: return "Ready"
        case .starting: return "Starting…"
        case .live: return "Live"
        case .paused: return "Paused"
        case .finishing: return "Finishing…"
        }
    }

    private var color: Color {
        switch status {
        case .idle: return .white.opacity(0.35)
        case .starting, .finishing: return Palette.mark
        case .live: return Color(red: 1.0, green: 0.27, blue: 0.23)
        case .paused: return Palette.mark.opacity(0.7)
        }
    }
}

private struct ToolbarButton: View {
    let symbol: String
    let help: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .semibold))
                .frame(width: 24, height: 20)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white.opacity(0.75))
        .background(.white.opacity(0.09), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
        .help(help)
        .accessibilityLabel(help)
    }
}

private struct CaptionStream: View {
    @ObservedObject var model: CaptionPanelModel
    let fontSize: Double

    private static let bottomID = "bottom"

    var body: some View {
        GeometryReader { viewport in
            ScrollViewReader { proxy in
                ScrollView(.vertical) {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(model.turns) { turn in
                            TurnView(turn: turn, fontSize: fontSize).id(turn.id)
                        }
                        Color.clear.frame(height: 1).id(Self.bottomID)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.top, 6)
                    .padding(.bottom, 12)
                    .background(GeometryReader { content in
                        Color.clear.preference(key: ContentBottomKey.self, value: content.frame(in: .named("stream")).maxY)
                    })
                }
                .coordinateSpace(name: "stream")
                .onPreferenceChange(ContentBottomKey.self) { bottom in
                    if bottom <= viewport.size.height + 24 { model.following = true }
                }
                .onChange(of: model.entries) { _ in
                    if model.following { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
                }
                .onChange(of: fontSize) { _ in
                    if model.following { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
                }
                .overlay(alignment: .bottomTrailing) {
                    if !model.following {
                        Button {
                            model.following = true
                            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
                        } label: {
                            Label("Latest", systemImage: "arrow.down")
                                .font(.system(size: 11, weight: .semibold))
                                .padding(.horizontal, 9)
                                .padding(.vertical, 5)
                                .background(.white.opacity(0.16), in: Capsule())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.white)
                        .padding(12)
                        .transition(.opacity)
                    }
                }
                .animation(.easeOut(duration: 0.15), value: model.following)
            }
        }
        .mask(
            VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 22)
                Color.black
            }
        )
        .overlay {
            if model.turns.isEmpty { EmptyState(status: model.status) }
        }
    }
}

private struct ContentBottomKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

private struct EmptyState: View {
    let status: CaptionPanelStatus

    var body: some View {
        VStack(spacing: 5) {
            Text(title)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.white.opacity(0.7))
            Text(detail)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.45))
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .allowsHitTesting(false)
    }

    private var title: String {
        switch status {
        case .idle: return "No session"
        case .starting: return "Starting capture…"
        case .live: return "Listening"
        case .paused: return "Paused"
        case .finishing: return "Finishing…"
        }
    }

    private var detail: String {
        switch status {
        case .idle: return "Captions appear here once a recording starts."
        case .starting: return "The capture helper is opening its streams."
        case .live: return "Captions appear here as people speak."
        case .paused: return "Resume to keep captioning."
        case .finishing: return "Writing the transcript."
        }
    }
}

private struct TurnView: View {
    let turn: CaptionTurn
    let fontSize: Double

    var body: some View {
        switch turn.kind {
        case .mark:
            HStack(spacing: 6) {
                Image(systemName: "bookmark.fill").font(.system(size: 9, weight: .bold))
                Text(turn.label)
                Text(CaptionPanelModel.clock(turn.startMilliseconds)).opacity(0.7)
            }
            .font(.system(size: max(11, fontSize - 5), weight: .semibold).monospacedDigit())
            .foregroundStyle(Palette.mark)
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
            .background(Palette.mark.opacity(0.14), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .accessibilityElement(children: .combine)
        case .speech(let source, _):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Circle().fill(source == .microphone ? Palette.microphone : Palette.system).frame(width: 6, height: 6)
                    Text(turn.label.uppercased()).kerning(0.7)
                    Text(CaptionPanelModel.clock(turn.startMilliseconds)).opacity(0.75)
                }
                .font(.system(size: max(10, fontSize - 7), weight: .semibold).monospacedDigit())
                .foregroundStyle(.white.opacity(0.55))
                .accessibilityElement(children: .combine)
                ForEach(turn.captions) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.text)
                            .font(.system(size: fontSize))
                            .foregroundStyle(item.isFinal ? .white : .white.opacity(0.55))
                            .accessibilityLabel(item.isFinal ? item.text : "Still speaking: \(item.text)")
                        if let translation = item.translation {
                            Text(translation)
                                .font(.system(size: max(Palette.minimumFontSize, fontSize - 3)))
                                .foregroundStyle(.white.opacity(0.62))
                        }
                    }
                    .lineSpacing(fontSize * 0.2)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                }
            }
        }
    }
}

/// Dark frosted background so captions stay readable over anything.
private struct HUDBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}
