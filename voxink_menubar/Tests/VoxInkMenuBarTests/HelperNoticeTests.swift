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
        XCTAssertEqual(
            "Microphone gain — AirPods input volume fell from 71% to 24%, probably by another app adjusting it; speech at this level may not be transcribed.",
            HelperNotice.line(from: "microphone_gain_dropped: AirPods input volume fell from 71% to 24%, probably by another app adjusting it; speech at this level may not be transcribed.")
        )
        XCTAssertEqual("Microphone gain — keeps being lowered", HelperNotice.line(from: "microphone_gain_hold_failed: keeps being lowered"))
        XCTAssertEqual("Microphone — No usable audio input", HelperNotice.line(from: "microphone_waiting: No usable audio input"))
        XCTAssertEqual("Microphone — Could not restart", HelperNotice.line(from: "microphone_reattach_failed: Could not restart"))
    }

    func testRecoveryAndCountersClearTheLine() {
        XCTAssertNil(HelperNotice.line(from: "service_reconnected: Stream microphone-1 reconnected to the service."))
        XCTAssertNil(HelperNotice.line(from: "audio_frames_dropped: Dropped 3 microphone frames (3 total)."))
        XCTAssertNil(HelperNotice.line(from: "microphone_reattached: Microphone capture reattached to AirPods at 24000 Hz after the default input device changed."))
        XCTAssertNil(HelperNotice.line(from: "microphone_gain_restored: AirPods input volume was lowered to 24%; put back to 71%."))
        XCTAssertNil(HelperNotice.line(from: "microphone_gain_changed: AirPods input volume is back at 71%."))
        XCTAssertNil(HelperNotice.line(from: "stream_silent: Stream system-1 has delivered no signal for 10 s."))
        XCTAssertNil(HelperNotice.line(from: "Traceback (most recent call last):"))
        XCTAssertNil(HelperNotice.line(from: nil))
    }
}
