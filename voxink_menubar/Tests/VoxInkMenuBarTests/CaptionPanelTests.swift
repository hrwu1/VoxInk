import AppKit
import XCTest
import VoxInkCapture
@testable import VoxInkMenuBar

@MainActor
final class CaptionPanelModelTests: XCTestCase {
    private func caption(
        _ id: String, _ text: String, at ms: Int64, source: AudioSource = .microphone,
        channel: String? = nil, final: Bool = true, translation: String? = nil, speaker: String? = nil
    ) -> CaptionItem {
        CaptionItem(
            id: id, source: source, text: text, startMilliseconds: ms, isFinal: final,
            channelName: channel, translation: translation, speakerName: speaker
        )
    }

    func testARecognizedVoiceNamesItsTurnAndANameChangeStartsANewOne() {
        let model = CaptionPanelModel()
        model.upsert(caption("a", "hello", at: 0))
        model.upsert(caption("b", "it's me", at: 1000, speaker: "Alice"))
        model.upsert(caption("c", "still me", at: 2000, speaker: "Alice"))
        model.upsert(caption("d", "and me", at: 3000, source: .system, channel: "Zoom", speaker: "Bob"))
        XCTAssertEqual(model.turns.map(\.label), ["Microphone", "Alice", "Bob"])
        XCTAssertEqual(model.turns[1].captions.map(\.id), ["b", "c"])
        XCTAssertTrue(model.transcriptText().contains("[00:01] Alice: it's me"))
    }

    func testAFinalReplacesItsPartialInPlaceAndKeepsAnEarlierTranslation() {
        let model = CaptionPanelModel()
        model.upsert(caption("a", "hel", at: 100, final: false))
        model.attachTranslation("你好", to: "a")
        model.upsert(caption("a", "hello", at: 100))
        XCTAssertEqual(model.entries.count, 1)
        guard case .caption(let item) = model.entries[0] else { return XCTFail("expected a caption") }
        XCTAssertEqual(item.text, "hello")
        XCTAssertTrue(item.isFinal)
        XCTAssertEqual(item.translation, "你好")
    }

    func testEntriesStayChronologicalAndMarksSortAmongCaptions() {
        let model = CaptionPanelModel()
        model.upsert(caption("b", "second", at: 2000))
        model.addMark(atMilliseconds: 1500, label: "decision")
        model.upsert(caption("a", "first", at: 1000))
        XCTAssertEqual(model.entries.map(\.startMilliseconds), [1000, 1500, 2000])
    }

    func testConsecutiveCaptionsFromOneSourceFormATurnAndMarksSplitThem() {
        let model = CaptionPanelModel()
        model.upsert(caption("a", "one", at: 0))
        model.upsert(caption("b", "two", at: 1000))
        model.upsert(caption("c", "three", at: 2000, source: .system, channel: "Zoom"))
        model.addMark(atMilliseconds: 2500, label: "")
        model.upsert(caption("d", "four", at: 3000, source: .system, channel: "Zoom"))
        let turns = model.turns
        XCTAssertEqual(turns.map(\.label), ["Microphone", "Zoom", "Marked", "Zoom"])
        XCTAssertEqual(turns[0].captions.map(\.id), ["a", "b"])
        XCTAssertEqual(turns[0].startMilliseconds, 0)
        XCTAssertEqual(turns[1].captions.map(\.id), ["c"])
        XCTAssertEqual(turns[3].captions.map(\.id), ["d"])
    }

    func testHistoryIsBoundedToTheNewestEntries() {
        let model = CaptionPanelModel()
        for index in 0..<(CaptionPanelModel.historyLimit + 5) {
            model.upsert(caption("s\(index)", "line \(index)", at: Int64(index) * 1000))
        }
        XCTAssertEqual(model.entries.count, CaptionPanelModel.historyLimit)
        XCTAssertEqual(model.entries.first?.id, "s5")
    }

    func testTranscriptTextCarriesClockLabelAndTranslation() {
        let model = CaptionPanelModel()
        model.upsert(caption("a", "hello", at: 65_000, translation: "你好"))
        model.addMark(atMilliseconds: 70_000, label: "action item")
        model.upsert(caption("b", "later", at: 3_600_000, source: .system))
        XCTAssertEqual(
            model.transcriptText(),
            "[01:05] Microphone: hello\n    你好\n[01:10] ◆ action item\n[1:00:00] System audio: later"
        )
    }

    func testClearingEmptiesTheStreamAndFollowsAgain() {
        let model = CaptionPanelModel()
        model.upsert(caption("a", "hello", at: 0))
        model.following = false
        model.clear()
        XCTAssertTrue(model.entries.isEmpty)
        XCTAssertTrue(model.following)
    }
}

final class CaptionPanelGeometryTests: XCTestCase {
    func testTheBubbleSitsAtTheBottomCentreAndExpandsBackToTheSameFrame() {
        let expanded = NSRect(x: 100, y: 50, width: 560, height: 220)
        let bubble = FloatingCaptionPanelController.bubbleFrame(for: expanded)
        XCTAssertEqual(bubble.midX, expanded.midX)
        XCTAssertEqual(bubble.minY, expanded.minY)
        XCTAssertEqual(bubble.size, NSSize(width: 40, height: 40))
        XCTAssertEqual(FloatingCaptionPanelController.expandedFrame(for: bubble, size: expanded.size), expanded)
    }

    func testADraggedBubbleCarriesTheExpandedFrameWithIt() {
        let moved = NSRect(x: 300, y: 500, width: 40, height: 40)
        let expanded = FloatingCaptionPanelController.expandedFrame(for: moved, size: NSSize(width: 400, height: 200))
        XCTAssertEqual(expanded, NSRect(x: 120, y: 500, width: 400, height: 200))
    }
}
