import Foundation

/// Ordered, bounded delivery of captured audio frames to the local service.
///
/// An audio callback must never block and must never allocate without bound,
/// yet the service's wire contract depends on frames arriving in capture order:
/// every JSON header is paired with the binary message that follows it. Frames
/// are therefore handed to a bounded `AsyncStream` that exactly one task drains
/// sequentially. Order is preserved, and a service that falls behind real time
/// costs bounded memory plus counted, reported drops instead of an unbounded
/// pile of concurrent send tasks.
final class AudioFrameQueue {
    /// Roughly ten seconds of 20 ms frames.
    static let defaultCapacity = 500

    private let sink: AudioFrameSink
    private let sessionID: String
    private let source: AudioSource
    private let capacity: Int
    private let dropReportInterval: UInt64
    private let lock = NSLock()
    private var continuation: AsyncStream<AudioFrame>.Continuation?
    private var consumer: Task<Void, Never>?
    private var droppedFrames = 0
    private var reportedDrops = 0
    private var lastReportNanoseconds: UInt64 = 0

    init(
        sink: AudioFrameSink,
        sessionID: String,
        source: AudioSource,
        capacity: Int = AudioFrameQueue.defaultCapacity,
        dropReportInterval: UInt64 = 1_000_000_000
    ) {
        self.sink = sink
        self.sessionID = sessionID
        self.source = source
        self.capacity = max(1, capacity)
        self.dropReportInterval = dropReportInterval
    }

    /// Total frames discarded because the service could not keep up.
    var droppedFrameCount: Int { withLock { droppedFrames } }

    var isRunning: Bool { withLock { continuation != nil } }

    /// Open the pipeline. Safe to call again after `finishAndDrain()` to resume.
    func start() {
        let (stream, continuation) = AsyncStream<AudioFrame>.makeStream(
            of: AudioFrame.self,
            bufferingPolicy: .bufferingNewest(capacity)
        )
        let opened = withLock { () -> Bool in
            guard self.continuation == nil else { return false }
            self.continuation = continuation
            return true
        }
        guard opened else {
            continuation.finish()
            return
        }
        let consumer = Task { [weak self] () -> Void in
            await self?.consume(stream)
        }
        withLock { self.consumer = consumer }
    }

    /// Enqueue one frame. Non-blocking, callable from an audio callback thread.
    ///
    /// Frames submitted while the pipeline is closed — before start or while
    /// paused — are discarded at the source rather than buffered, because
    /// replaying stale audio after a resume would corrupt the session timeline.
    func submit(_ frame: AudioFrame) {
        guard let continuation = withLock({ self.continuation }) else { return }
        // `bufferingNewest` evicts the oldest queued frame, keeping latency low
        // and leaving the gap behind the listener rather than ahead of them.
        if case .dropped = continuation.yield(frame) {
            withLock { droppedFrames += 1 }
        }
    }

    /// Close the pipeline and wait until every buffered frame has been sent.
    func finishAndDrain() async {
        let (continuation, consumer) = withLock { () -> (AsyncStream<AudioFrame>.Continuation?, Task<Void, Never>?) in
            let pair = (self.continuation, self.consumer)
            self.continuation = nil
            self.consumer = nil
            return pair
        }
        continuation?.finish()
        await consumer?.value
    }

    private func consume(_ stream: AsyncStream<AudioFrame>) async {
        for await frame in stream {
            try? await sink.send(frame)
            await reportDropsIfNeeded(force: false)
        }
        await reportDropsIfNeeded(force: true)
    }

    /// Report accumulated drops at most once per interval so a sustained
    /// overload produces a readable warning rather than a flood of them.
    private func reportDropsIfNeeded(force: Bool) async {
        let now = DispatchTime.now().uptimeNanoseconds
        let report = withLock { () -> (pending: Int, total: Int)? in
            let pending = droppedFrames - reportedDrops
            let elapsed = now >= lastReportNanoseconds ? now - lastReportNanoseconds : 0
            guard pending > 0, force || lastReportNanoseconds == 0 || elapsed >= dropReportInterval else {
                return nil
            }
            reportedDrops = droppedFrames
            lastReportNanoseconds = now
            return (pending, droppedFrames)
        }
        guard let report else { return }
        let pending = report.pending
        let total = report.total

        try? await sink.send(CaptureStatusEvent(
            sessionID: sessionID,
            source: source,
            level: .warning,
            code: "audio_frames_dropped",
            message: "Dropped \(pending) \(source.rawValue) frames because transcription fell behind real time (\(total) total)."
        ))
    }

    /// Scoped locking: `NSLock.lock()` is unavailable directly from an async
    /// context, and nothing here holds the lock across a suspension point.
    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }
}
