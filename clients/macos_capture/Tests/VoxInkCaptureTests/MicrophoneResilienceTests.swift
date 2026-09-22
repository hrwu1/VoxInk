import XCTest
@testable import VoxInkCapture

final class InputGainPolicyTests: XCTestCase {
    func testSmallWobbleIsIgnoredAndADropUnderTheRatioIsAWarning() {
        var policy = InputGainPolicy(reference: 0.71, hold: false)
        XCTAssertEqual(.unchanged, policy.observe(gain: 0.72, now: 1))
        // 0.50 is 70% of the start level: worth a line, not a warning.
        XCTAssertEqual(.changed(warning: false), policy.observe(gain: 0.50, now: 2))
        // 0.24 is what Zoom left the AirPods at: under 60%, captions go missing.
        XCTAssertEqual(.changed(warning: true), policy.observe(gain: 0.24, now: 3))
        XCTAssertEqual(.recovered, policy.observe(gain: 0.71, now: 4))
        XCTAssertEqual(.unchanged, policy.observe(gain: 0.71, now: 5))
    }

    func testARaiseIsReportedButNeverHeld() {
        var policy = InputGainPolicy(reference: 0.5, hold: true)
        XCTAssertEqual(.changed(warning: false), policy.observe(gain: 0.8, now: 1))
        XCTAssertTrue(policy.hold)
    }

    func testHoldRestoresADropAndSwallowsTheDeviceEchoingIt() {
        var policy = InputGainPolicy(reference: 0.71, hold: true)
        XCTAssertEqual(.restore, policy.observe(gain: 0.24, now: 10))
        // The listener fires again with the value the restore set.
        XCTAssertEqual(.unchanged, policy.observe(gain: 0.71, now: 10.1))
        // A second drop inside the cooldown is only reported.
        XCTAssertEqual(.changed(warning: true), policy.observe(gain: 0.24, now: 12))
        XCTAssertEqual(.recovered, policy.observe(gain: 0.71, now: 13))
        // Past the cooldown it is restored again.
        XCTAssertEqual(.restore, policy.observe(gain: 0.30, now: 16))
    }

    func testHoldGivesUpAfterFiveRestoresInAMinute() {
        var policy = InputGainPolicy(reference: 0.7, hold: true)
        var now = 0.0
        for _ in 0..<5 {
            XCTAssertEqual(.restore, policy.observe(gain: 0.2, now: now))
            XCTAssertEqual(.unchanged, policy.observe(gain: 0.7, now: now + 0.1))
            now += 6
        }
        XCTAssertEqual(.holdExhausted, policy.observe(gain: 0.2, now: now))
        XCTAssertFalse(policy.hold)
        // From here on changes are reported only.
        XCTAssertEqual(.recovered, policy.observe(gain: 0.7, now: now + 1))
        XCTAssertEqual(.changed(warning: true), policy.observe(gain: 0.2, now: now + 2))
    }

    func testAFailedRestoreStopsHoldingWithoutSwallowingTheNextChange() {
        var policy = InputGainPolicy(reference: 0.7, hold: true)
        XCTAssertEqual(.restore, policy.observe(gain: 0.2, now: 1))
        policy.restoreFailed()
        XCTAssertFalse(policy.hold)
        XCTAssertEqual(.recovered, policy.observe(gain: 0.7, now: 2))
    }
}

final class StreamActivityTrackerTests: XCTestCase {
    func testSilenceIsReportedOnceAfterTheDelayAndSignalOnceWhenItReturns() {
        var tracker = StreamActivityTracker(silenceThreshold: 8, silentAfterSeconds: 10)
        XCTAssertNil(tracker.observe(peak: 1200, atSeconds: 0))
        XCTAssertNil(tracker.observe(peak: 0, atSeconds: 1))
        XCTAssertNil(tracker.observe(peak: 3, atSeconds: 9.9))
        XCTAssertEqual(.silent, tracker.observe(peak: 0, atSeconds: 11))
        XCTAssertNil(tracker.observe(peak: 0, atSeconds: 30))
        XCTAssertEqual(.active(silentSeconds: 41), tracker.observe(peak: 500, atSeconds: 42))
        XCTAssertNil(tracker.observe(peak: 500, atSeconds: 43))
    }

    func testABriefGapDoesNotCount() {
        var tracker = StreamActivityTracker(silenceThreshold: 8, silentAfterSeconds: 10)
        XCTAssertNil(tracker.observe(peak: 0, atSeconds: 0))
        XCTAssertNil(tracker.observe(peak: 300, atSeconds: 5))
        XCTAssertNil(tracker.observe(peak: 0, atSeconds: 6))
        XCTAssertNil(tracker.observe(peak: 0, atSeconds: 15))
        XCTAssertEqual(.silent, tracker.observe(peak: 0, atSeconds: 16))
    }

    func testQuietRoomNoiseIsSignal() {
        var tracker = StreamActivityTracker()
        for second in 0..<30 {
            XCTAssertNil(tracker.observe(peak: 80, atSeconds: Double(second)))
        }
    }

    func testLinesNameTheStream() {
        let tracker = StreamActivityTracker()
        let silent = tracker.describe(.silent, streamID: "microphone-1")
        XCTAssertEqual("stream_silent", silent.code)
        XCTAssertEqual("Stream microphone-1 has delivered no signal for 10 s.", silent.message)
        let active = tracker.describe(.active(silentSeconds: 41.6), streamID: "system-1")
        XCTAssertEqual("stream_active", active.code)
        XCTAssertEqual("Stream system-1 is delivering signal again after 42 s without any.", active.message)
    }
}

final class HoldGainConfigurationTests: XCTestCase {
    func testHoldingTheMicrophoneGainIsOffUnlessAsked() {
        let plain = CaptureConfiguration(sessionID: "s", microphoneEnabled: true, systemAudio: nil)
        XCTAssertFalse(plain.holdMicrophoneGain)
        let held = CaptureConfiguration(sessionID: "s", microphoneEnabled: true, systemAudio: nil, holdMicrophoneGain: true)
        XCTAssertTrue(held.holdMicrophoneGain)
    }
}
