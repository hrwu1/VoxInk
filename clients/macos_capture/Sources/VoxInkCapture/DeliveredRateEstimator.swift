import Foundation

/// Measures the sample rate a Core Audio device is actually delivering.
///
/// A process tap declares a format when it is created, but the audio that
/// arrives through the aggregate device is clocked by the output device, and
/// the two disagree: on this Mac a tap declared 48 kHz while a Bluetooth
/// headset delivered 44.1 kHz, and 16 kHz once the headset entered its call
/// profile. Resampling by the declared ratio then discards most of the signal
/// — at 16 kHz declared as 48 kHz, two thirds of it — and nothing in the HAL
/// reports the mismatch. `kAudioTapPropertyFormat` and the aggregate's input
/// virtual format were both wrong; only the nominal rate agreed, and only by
/// luck, so a property is a starting guess rather than an answer.
///
/// Frames delivered divided by elapsed time is the delivered rate by
/// definition. Both numbers come from the IO proc's own `AudioTimeStamp`
/// rather than from when our thread happened to run, so the estimate does not
/// need to average away callback jitter — it only needs enough evidence to
/// rule out a burst.
///
/// Not thread-safe: the caller drives it from one audio callback thread, or
/// holds its own lock.
struct DeliveredRateEstimator {
    /// Rates a device is likely to mean when the measurement lands near one.
    /// A measurement inside `snapTolerance` of one of these is reported as the
    /// exact value, so a converter is not rebuilt for a 44100.3 Hz reading.
    static let standardRates: [Double] = [
        8_000, 11_025, 16_000, 22_050, 24_000, 32_000,
        44_100, 48_000, 64_000, 88_200, 96_000, 176_400, 192_000,
    ]

    /// Callbacks further apart than this did not come from one continuous
    /// stream. A tap only runs while something is playing, so a longer gap is
    /// silence, and averaging across it would read as a far lower rate.
    var maximumCallbackGap: TimeInterval = 0.25
    /// Evidence required before a correction: enough seconds that a burst
    /// cannot dominate, and enough callbacks that one long buffer cannot.
    var minimumWindow: TimeInterval = 1.0
    var minimumCallbacks: Int = 20
    /// How far a measurement may sit from a standard rate and still be read as
    /// that rate. 5% is comfortably below the 8.4% gap between the closest
    /// pair that matters here (44.1 and 48 kHz).
    var snapTolerance: Double = 0.05
    /// A measurement must differ from the rate in use by more than this before
    /// it is worth rebuilding the converter.
    var correctionTolerance: Double = 0.02

    private var frames: Double = 0
    private var seconds: TimeInterval = 0
    private var callbacks: Int = 0
    private var lastHostSeconds: TimeInterval?

    init() {}

    /// Forget the window in progress, keeping the configured thresholds.
    ///
    /// Called when the stream it was measuring is gone — the tap was rebuilt,
    /// or delivery paused — so that evidence from the old device cannot be
    /// averaged with evidence from the new one.
    mutating func reset() {
        frames = 0
        seconds = 0
        callbacks = 0
        lastHostSeconds = nil
    }

    /// Record one delivery, and return a rate to adopt when there is one.
    ///
    /// - Parameters:
    ///   - frameCount: frames in this callback's buffer.
    ///   - hostSeconds: the callback's host timestamp, in seconds.
    ///   - assumedRate: the rate the caller is currently converting at.
    /// - Returns: the rate to switch to, or nil to carry on unchanged.
    mutating func observe(
        frameCount: Int,
        hostSeconds: TimeInterval,
        assumedRate: Double
    ) -> Double? {
        defer { lastHostSeconds = hostSeconds }
        guard frameCount > 0 else { return nil }
        guard let previous = lastHostSeconds else { return nil }

        let elapsed = hostSeconds - previous
        // A host clock that did not advance says nothing, and one that went
        // backwards means the timestamps are not usable as a clock at all.
        guard elapsed > 0, elapsed <= maximumCallbackGap else {
            frames = 0
            seconds = 0
            callbacks = 0
            return nil
        }

        // The frames counted are the ones delivered *over* this interval, so
        // this callback's buffer is what filled it.
        frames += Double(frameCount)
        seconds += elapsed
        callbacks += 1

        guard seconds >= minimumWindow, callbacks >= minimumCallbacks else { return nil }
        let measured = frames / seconds
        frames = 0
        seconds = 0
        callbacks = 0

        let candidate = Self.snapToStandardRate(measured, tolerance: snapTolerance)
        guard assumedRate > 0 else { return candidate }
        guard abs(candidate - assumedRate) / assumedRate > correctionTolerance else { return nil }
        return candidate
    }

    /// The standard rate within `tolerance` of `measured`, else `measured`
    /// rounded to whole hertz so an unusual device is still described exactly.
    static func snapToStandardRate(_ measured: Double, tolerance: Double) -> Double {
        var best: Double?
        var bestDistance = Double.infinity
        for rate in standardRates {
            let distance = abs(measured - rate) / rate
            if distance <= tolerance, distance < bestDistance {
                best = rate
                bestDistance = distance
            }
        }
        return best ?? measured.rounded()
    }
}
