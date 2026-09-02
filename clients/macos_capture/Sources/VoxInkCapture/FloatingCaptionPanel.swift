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

/// Minimal always-on-top caption surface for Phase 2/3 integration. A host app
/// feeds normalized partial/final transcript events through `show(_:)`.
@MainActor
public final class FloatingCaptionPanelController {
    private let model = CaptionPanelModel()
    private let panel: NSPanel

    public init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 210),
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
        panel.isMovableByWindowBackground = true
        panel.contentView = NSHostingView(rootView: FloatingCaptionView(model: model))
    }

    public func show(_ item: CaptionItem) {
        model.upsert(item)
        if !panel.isVisible {
            panel.center()
            panel.orderFrontRegardless()
        }
    }

    /// Attach a translation that arrived after its caption was shown.
    public func attachTranslation(_ translation: String, to id: String) {
        model.attachTranslation(translation, to: id)
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
        model.items.removeAll()
    }
}

@MainActor
private final class CaptionPanelModel: ObservableObject {
    @Published var items = [CaptionItem]()

    func attachTranslation(_ translation: String, to id: String) {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return }
        items[index] = items[index].withTranslation(translation)
    }

    func upsert(_ item: CaptionItem) {
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            // A final that replaces a partial keeps a translation that already
            // arrived for the same segment.
            let existing = items[index]
            items[index] = item.translation == nil && existing.translation != nil
                ? item.withTranslation(existing.translation)
                : item
        } else {
            items.append(item)
        }
        items.sort {
            if $0.startMilliseconds == $1.startMilliseconds { return $0.id < $1.id }
            return $0.startMilliseconds < $1.startMilliseconds
        }
        if items.count > 4 { items.removeFirst(items.count - 4) }
    }
}

private struct FloatingCaptionView: View {
    @ObservedObject var model: CaptionPanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(model.items) { item in
                HStack(alignment: .top, spacing: 10) {
                    Text(item.source == .microphone ? "MIC" : (item.channelName?.uppercased() ?? "SYSTEM"))
                        .font(.system(size: 10, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 4)
                        .background(item.source == .microphone ? Color.blue : Color.purple, in: Capsule())
                        .accessibilityLabel(item.source == .microphone ? "Microphone" : (item.channelName ?? "System audio"))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.text)
                            .font(.system(size: 18, weight: item.isFinal ? .regular : .light))
                            .foregroundStyle(item.isFinal ? .white : .white.opacity(0.78))
                            .lineLimit(2)
                        if let translation = item.translation {
                            Text(translation)
                                .font(.system(size: 15, weight: .light))
                                .foregroundStyle(.white.opacity(0.7))
                                .lineLimit(2)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeOut(duration: 0.16), value: model.items)
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
        .background(.black.opacity(0.78), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}
