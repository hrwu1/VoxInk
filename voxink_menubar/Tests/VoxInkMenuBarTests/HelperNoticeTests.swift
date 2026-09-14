import XCTest
@testable import VoxInkMenuBar

final class HelperNoticeTests: XCTestCase {
    func testDegradedRecordingLinesAreShownWithAPlainLabel() {
        XCTAssertEqual(
            "Audio saved locally — Stream microphone-1 cannot reach the service; its audio is being saved to /tmp/x.wav and is not in the live transcript.",
            HelperNotice.line(from: "audio_spooled: Stream microphone-1 cannot reach the service; its audio is being saved to /tmp/x.wav and is not in the live transcript.")
        )
        XCTAssertEqual(
            "Service connection — Reconnecting stream microphone-1 to the service (attempt 2); audio is kept until it is back.",
            HelperNotice.line(from: "service_reconnecting: Reconnecting stream microphone-1 to the service (attempt 2); audio is kept until it is back.")
        )
        XCTAssertEqual("Audio is being lost — disk full", HelperNotice.line(from: "audio_spool_failed: disk full"))
        XCTAssertEqual("Live speakers — stopped", HelperNotice.line(from: "live_speakers_stopped: stopped"))
    }

    func testRecoveryAndCountersClearTheLine() {
        XCTAssertNil(HelperNotice.line(from: "service_reconnected: Stream microphone-1 reconnected to the service."))
        XCTAssertNil(HelperNotice.line(from: "audio_frames_dropped: Dropped 3 microphone frames (3 total)."))
        XCTAssertNil(HelperNotice.line(from: "Traceback (most recent call last):"))
        XCTAssertNil(HelperNotice.line(from: nil))
    }
}
