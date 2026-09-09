import XCTest
@testable import VoxInkMenuBar

final class RecordingShortcutTests: XCTestCase {
    private var suite: String!
    private var defaults: UserDefaults!

    override func setUp() {
        suite = "VoxInkHotkeyTests." + UUID().uuidString
        defaults = UserDefaults(suiteName: suite)!
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
    }

    func testStartsIdleAndStopsRecordingOrPausedSessions() {
        for (state, expected) in [
            ("ready", RecordingShortcut.Action.start), ("failed", .start),
            ("recording", .stop), ("paused", .stop),
        ] {
            var shortcut = RecordingShortcut(defaults: defaults)
            XCTAssertEqual(shortcut.press(state: state, pushToTalkArmed: false, busy: false), expected)
        }
    }

    func testHeldKeyCannotStopTheRecordingItJustStarted() {
        var shortcut = RecordingShortcut(defaults: defaults)
        XCTAssertEqual(shortcut.press(state: "ready", pushToTalkArmed: false, busy: false), .start)
        XCTAssertNil(shortcut.press(state: "recording", pushToTalkArmed: false, busy: false))
        shortcut.release()
        XCTAssertEqual(shortcut.press(state: "recording", pushToTalkArmed: false, busy: false), .stop)
        XCTAssertNil(shortcut.press(state: "ready", pushToTalkArmed: false, busy: false))
    }

    func testPressDuringTransitionIsNotQueuedForLater() {
        for state in ["starting", "stopping", "unknown"] {
            var shortcut = RecordingShortcut(defaults: defaults)
            XCTAssertNil(shortcut.press(state: state, pushToTalkArmed: false, busy: false))
            XCTAssertNil(shortcut.press(state: "recording", pushToTalkArmed: false, busy: false))
            shortcut.release()
            XCTAssertEqual(shortcut.press(state: "recording", pushToTalkArmed: false, busy: false), .stop)
        }
    }

    func testPendingCommandAndPushToTalkBlockNormalRecording() {
        for state in ["ready", "failed", "recording", "paused"] {
            XCTAssertNil(RecordingShortcut.action(state: state, pushToTalkArmed: true, busy: false))
            XCTAssertNil(RecordingShortcut.action(state: state, pushToTalkArmed: false, busy: true))
        }
    }

    func testSourcePersistsAcrossShortcutInstances() {
        let shortcut = RecordingShortcut(defaults: defaults)
        XCTAssertEqual(shortcut.sources, ["microphone"])
        for sources in [["system"], ["microphone", "system"], ["microphone"]] {
            shortcut.remember(sources: sources)
            XCTAssertEqual(RecordingShortcut(defaults: defaults).sources, sources)
        }
    }

    func testInvalidSavedSourceFallsBackToMicrophone() {
        for invalid: Any in [[], ["applications"], ["microphone", "invalid"], "system"] {
            defaults.set(invalid, forKey: "recordingShortcutSources")
            XCTAssertEqual(RecordingShortcut(defaults: defaults).sources, ["microphone"])
        }
    }

    func testInvalidSelectionCannotOverwriteSavedSource() {
        let shortcut = RecordingShortcut(defaults: defaults)
        shortcut.remember(sources: ["system"])
        shortcut.remember(sources: [])
        XCTAssertEqual(shortcut.sources, ["system"])
    }

    func testHotkeyIDsKeepRecordingSeparateFromDictation() {
        XCTAssertEqual(MenuBarHotKey(signature: MenuBarHotKey.signature, id: 1), .pushToTalk)
        XCTAssertEqual(MenuBarHotKey(signature: MenuBarHotKey.signature, id: 2), .recording)
        XCTAssertNil(MenuBarHotKey(signature: MenuBarHotKey.signature, id: 3))
        XCTAssertNil(MenuBarHotKey(signature: 0, id: 2))
    }
}
