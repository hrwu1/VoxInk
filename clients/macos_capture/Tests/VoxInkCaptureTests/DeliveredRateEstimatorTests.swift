import XCTest
@testable import VoxInkCapture

/// Drives the estimator with a synthetic device: `frameCount` frames handed
/// over every `frameCount / trueRate` seconds, which is what an IO proc on a
/// device running at `trueRate` does.
private func feed(
    _ estimator: inout DeliveredRateEstimator,
    trueRate: Double,
    assumedRate: Double,
    frameCount: Int = 512,
    seconds: TimeInterval,
    startingAt start: TimeInterval = 1_000
) -> Double? {
    let interval = Double(frameCount) / trueRate
    var host = start
    var result: Double?
    for _ in 0..<Int((seconds / interval).rounded()) {
        host += interval
        if let rate = estimator.observe(
            frameCount: frameCount, hostSeconds: host, assumedRate: assumedRate
        ), result == nil {
            result = rate
        }
    }
    return result
}

final class DeliveredRateEstimatorTests: XCTestCase {
    func testReportsTheDeliveredRateWhenTheAssumedRateIsWrong() {
        // The defect this exists for: a tap declaring 48 kHz over a headset
        // that is really delivering 16 kHz in its call profile.
        var estimator = DeliveredRateEstimator()
        let corrected = feed(&estimator, trueRate: 16_000, assumedRate: 48_000, seconds: 3)
        XCTAssertEqual(corrected, 16_000)
    }

    func testReportsFortyFourOneAgainstADeclaredFortyEight() {
        var estimator = DeliveredRateEstimator()
        let corrected = feed(&estimator, trueRate: 44_100, assumedRate: 48_000, seconds: 3)
        XCTAssertEqual(corrected, 44_100)
    }

    func testStaysSilentWhenTheAssumedRateIsAlreadyRight() {
        var estimator = DeliveredRateEstimator()
        XCTAssertNil(feed(&estimator, trueRate: 48_000, assumedRate: 48_000, seconds: 5))
    }

    func testToleratesJitterAroundTheCorrectRate() {
        // Real callbacks do not arrive on an exact period; a rate that is right
        // must not be "corrected" because of it.
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        var generator = SystemRandomNumberGenerator()
        for _ in 0..<600 {
            host += (512.0 / 48_000.0) + Double.random(in: -0.0008...0.0008, using: &generator)
            XCTAssertNil(
                estimator.observe(frameCount: 512, hostSeconds: host, assumedRate: 48_000)
            )
        }
    }

    func testWaitsForEnoughEvidenceBeforeCorrecting() {
        // Half a second of callbacks is a burst, not a measurement.
        var estimator = DeliveredRateEstimator()
        XCTAssertNil(feed(&estimator, trueRate: 16_000, assumedRate: 48_000, seconds: 0.5))
    }

    func testRequiresSeveralCallbacksNotJustElapsedTime() {
        // One enormous buffer spanning the window is not evidence of a rate.
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        for _ in 0..<5 {
            host += 0.24
            XCTAssertNil(
                estimator.observe(frameCount: 3_840, hostSeconds: host, assumedRate: 48_000)
            )
        }
    }

    func testSilenceBetweenBurstsDoesNotReadAsALowerRate() {
        // A tap only runs while audio plays. Averaging across the quiet would
        // make a correct 48 kHz device look far slower than it is.
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        for burst in 0..<10 {
            if burst > 0 { host += 5 }  // nothing played for five seconds
            for _ in 0..<30 {
                host += 512.0 / 48_000.0
                XCTAssertNil(
                    estimator.observe(frameCount: 512, hostSeconds: host, assumedRate: 48_000)
                )
            }
        }
    }

    func testRecoversAfterAGapAndStillCorrects() {
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        _ = estimator.observe(frameCount: 512, hostSeconds: host, assumedRate: 48_000)
        host += 30  // a long silence mid-session
        var corrected: Double?
        for _ in 0..<200 {
            host += 512.0 / 16_000.0
            if let rate = estimator.observe(
                frameCount: 512, hostSeconds: host, assumedRate: 48_000
            ), corrected == nil {
                corrected = rate
            }
        }
        XCTAssertEqual(corrected, 16_000)
    }

    func testIgnoresATimestampThatDoesNotAdvance() {
        var estimator = DeliveredRateEstimator()
        for _ in 0..<100 {
            XCTAssertNil(
                estimator.observe(frameCount: 512, hostSeconds: 1_000, assumedRate: 48_000)
            )
        }
    }

    func testIgnoresATimestampThatGoesBackwards() {
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        for _ in 0..<100 {
            host -= 0.01
            XCTAssertNil(
                estimator.observe(frameCount: 512, hostSeconds: host, assumedRate: 48_000)
            )
        }
    }

    func testResetForgetsTheWindowInProgress() {
        var estimator = DeliveredRateEstimator()
        var host: TimeInterval = 1_000
        for _ in 0..<25 {
            host += 512.0 / 16_000.0
            _ = estimator.observe(frameCount: 512, hostSeconds: host, assumedRate: 48_000)
        }
        estimator.reset()
        // Too little evidence again now that the window is gone.
        var corrected: Double?
        for _ in 0..<5 {
            host += 512.0 / 16_000.0
            if let rate = estimator.observe(
                frameCount: 512, hostSeconds: host, assumedRate: 48_000
            ) { corrected = rate }
        }
        XCTAssertNil(corrected)
    }

    func testAnUnusualRateIsReportedRatherThanForcedToAStandardOne() {
        var estimator = DeliveredRateEstimator()
        let corrected = feed(&estimator, trueRate: 37_800, assumedRate: 48_000, seconds: 3)
        XCTAssertEqual(try XCTUnwrap(corrected), 37_800, accuracy: 1)
    }

    func testSnappingPrefersTheNearestStandardRate() {
        XCTAssertEqual(
            DeliveredRateEstimator.snapToStandardRate(44_101.7, tolerance: 0.05), 44_100
        )
        XCTAssertEqual(
            DeliveredRateEstimator.snapToStandardRate(47_600, tolerance: 0.05), 48_000
        )
        XCTAssertEqual(
            DeliveredRateEstimator.snapToStandardRate(37_800, tolerance: 0.05), 37_800
        )
    }
}
