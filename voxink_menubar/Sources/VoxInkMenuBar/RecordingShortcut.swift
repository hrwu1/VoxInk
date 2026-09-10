import Foundation

/// The recording shortcut's saved source and one-action-per-press policy.
/// Command execution and platform hotkey registration stay in the controller.
struct RecordingShortcut {
    enum Action: Equatable { case start, stop }

    static let sourceOptions: [(title: String, sources: [String])] = [
        ("Microphone", ["microphone"]),
        ("Microphone + System audio", ["microphone", "system"]),
        ("System audio", ["system"]),
    ]
    private let defaults: UserDefaults
    private var held = false

    init(defaults: UserDefaults) { self.defaults = defaults }

    var sources: [String] {
        let saved = defaults.stringArray(forKey: "recordingShortcutSources")
        return Self.sourceOptions.first { $0.sources == saved }?.sources ?? ["microphone"]
    }

    func remember(sources: [String]) {
        guard Self.sourceOptions.contains(where: { $0.sources == sources }) else { return }
        defaults.set(sources, forKey: "recordingShortcutSources")
    }

    static func action(state: String, pushToTalkArmed: Bool, busy: Bool) -> Action? {
        guard !pushToTalkArmed, !busy else { return nil }
        switch state {
        case "ready", "failed": return .start
        case "recording", "paused": return .stop
        default: return nil
        }
    }

    mutating func press(state: String, pushToTalkArmed: Bool, busy: Bool) -> Action? {
        guard !held else { return nil }
        held = true
        return Self.action(state: state, pushToTalkArmed: pushToTalkArmed, busy: busy)
    }

    mutating func release() { held = false }
}

enum MenuBarHotKey: UInt32 {
    case pushToTalk = 1
    case recording = 2

    static let signature: UInt32 = 0x5658_494B // "VXIK"

    init?(signature: UInt32, id: UInt32) {
        guard signature == Self.signature else { return nil }
        self.init(rawValue: id)
    }
}
