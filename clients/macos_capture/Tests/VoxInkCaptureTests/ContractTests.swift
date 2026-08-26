import AVFoundation
import XCTest
@testable import VoxInkCapture

final class ContractTests: XCTestCase {
    func testTranscriptWireEventMapsToCaption() throws {
        let json = Data(#"{"segment_id":"seg-1","source":"system","text":"hello","start_ms":10,"end_ms":30,"is_final":true}"#.utf8)
        let event = try JSONDecoder().decode(TranscriptWireEvent.self, from: json)
        XCTAssertEqual(event.captionItem.id, "seg-1")
        XCTAssertEqual(event.captionItem.source, .system)
        XCTAssertEqual(event.captionItem.text, "hello")
        XCTAssertTrue(event.captionItem.isFinal)
    }

    func testDefaultEndpointIncludesSessionID() {
        let configuration = CaptureConfiguration(
            sessionID: "session-1",
            microphoneEnabled: true,
            systemAudio: nil
        )
        XCTAssertEqual(
            configuration.serviceURL.absoluteString,
            "ws://127.0.0.1:8765/v1/sessions/session-1/stream"
        )
    }

    func testEndpointPolicyOnlyAllowsLoopbackWebSockets() throws {
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "ws://127.0.0.1:8765/stream")!))
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "wss://localhost/stream")!))
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "ws://[::1]:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "ws://0.0.0.0:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "ws://192.168.1.10:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "https://127.0.0.1/stream")!))
    }

    func testPCMNormalizerDownmixesAndResamples() throws {
        let normalizer = PCMNormalizer(outputSampleRate: 16_000)
        let data = try normalizer.normalize(Self.stereoBuffer(frames: 480))
        XCTAssertGreaterThan(data.count, 0)
        XCTAssertLessThanOrEqual(data.count, 384)
        XCTAssertEqual(data.count % 2, 0)
    }

    /// A retained converter keeps its resampler state: the filter is primed
    /// once (measured as 6 frames on macOS 26) and every later 480-frame buffer
    /// then yields exactly 160 frames. Rebuilding the converter per buffer
    /// instead restarts the filter at every boundary.
    func testPCMNormalizerReusesConverterAcrossBuffers() throws {
        let normalizer = PCMNormalizer(outputSampleRate: 16_000)
        var counts = [Int]()
        for _ in 0..<8 {
            counts.append(try normalizer.normalize(Self.stereoBuffer(frames: 480)).count / 2)
        }
        let ideal = 480 * 8 / 3
        let produced = counts.reduce(0, +)
        XCTAssertLessThanOrEqual(produced, ideal, "resampling must not invent frames")
        XCTAssertGreaterThanOrEqual(produced, ideal - 32, "only priming latency may be outstanding")
        XCTAssertEqual(Array(counts.suffix(4)), [160, 160, 160, 160])
    }

    func testSystemPCMLevelSummaryReadsSignedLittleEndianSamples() {
        let pcm = Data([0x00, 0x00, 0xD2, 0x04, 0x30, 0xF8]) // 0, 1234, -2000
        XCTAssertEqual(SystemAudioCapture.pcmPeakAmplitude(pcm), 2000)
        let buffer = Self.stereoBuffer(frames: 480)
        XCTAssertEqual(
            SystemAudioCapture.pcmPeakAmplitude(buffer),
            0.25,
            accuracy: 0.0001
        )
        let activity = SystemAudioCapture.pcmByteActivity(buffer)
        XCTAssertGreaterThan(activity.nonzero, 0)
        XCTAssertGreaterThan(activity.total, 0)
    }

    // MARK: - Ordered, bounded frame delivery

    func testFramesArriveInSubmissionOrderWhileTheSinkIsSlow() async {
        let sink = RecordingSink(sendDelayNanoseconds: 200_000)
        let queue = AudioFrameQueue(sink: sink, sessionID: "s", source: .microphone, capacity: 512)
        queue.start()
        for sequence in 1...200 {
            queue.submit(Self.frame(sequence: UInt64(sequence)))
        }
        await queue.finishAndDrain()

        XCTAssertEqual(queue.droppedFrameCount, 0)
        XCTAssertEqual(sink.sequences, Array(1...200).map(UInt64.init))
    }

    func testABoundedQueueDropsAndReportsInsteadOfGrowing() async {
        let sink = RecordingSink(sendDelayNanoseconds: 2_000_000)
        let queue = AudioFrameQueue(sink: sink, sessionID: "s", source: .microphone, capacity: 4)
        queue.start()
        for sequence in 1...100 {
            queue.submit(Self.frame(sequence: UInt64(sequence)))
        }
        await queue.finishAndDrain()

        XCTAssertGreaterThan(queue.droppedFrameCount, 0, "a slow sink must cost drops, not memory")
        XCTAssertLessThan(sink.sequences.count, 100)
        XCTAssertEqual(sink.sequences.count + queue.droppedFrameCount, 100)
        XCTAssertEqual(sink.sequences, sink.sequences.sorted(), "surviving frames stay in order")
        XCTAssertTrue(
            sink.statusEvents.contains { $0.code == "audio_frames_dropped" && $0.level == .warning },
            "drops must be reported, not silent"
        )
    }

    func testPauseStopsDeliveryAndResumeRestartsIt() async {
        let sink = RecordingSink()
        let queue = AudioFrameQueue(sink: sink, sessionID: "s", source: .microphone, capacity: 64)

        queue.start()
        for sequence in 1...3 { queue.submit(Self.frame(sequence: UInt64(sequence))) }
        await queue.finishAndDrain()
        XCTAssertEqual(sink.sequences, [1, 2, 3])

        // Paused: audio is refused at the source rather than buffered, so a
        // resume cannot replay stale audio onto the session timeline.
        for sequence in 4...6 { queue.submit(Self.frame(sequence: UInt64(sequence))) }
        XCTAssertEqual(sink.sequences, [1, 2, 3])
        XCTAssertEqual(queue.droppedFrameCount, 0, "a deliberate pause is not an overload")

        queue.start()
        for sequence in 7...8 { queue.submit(Self.frame(sequence: UInt64(sequence))) }
        await queue.finishAndDrain()
        XCTAssertEqual(sink.sequences, [1, 2, 3, 7, 8])
    }

    // MARK: - Helpers

    private static func frame(sequence: UInt64) -> AudioFrame {
        AudioFrame(
            sessionID: "s",
            streamID: "microphone-1",
            source: .microphone,
            sequence: sequence,
            timestampNanoseconds: sequence * 20_000_000,
            sampleRate: 16_000,
            pcmS16LE: Data(count: 640)
        )
    }

    private static func stereoBuffer(frames: AVAudioFrameCount) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<2 {
            for frame in 0..<Int(frames) {
                buffer.floatChannelData?[channel][frame] = channel == 0 ? 0.25 : -0.25
            }
        }
        return buffer
    }
}

/// Records what the pipeline delivered, optionally taking its time about it.
private final class RecordingSink: AudioFrameSink, @unchecked Sendable {
    private let sendDelayNanoseconds: UInt64
    private let lock = NSLock()
    private var recordedSequences = [UInt64]()
    private var recordedStatusEvents = [CaptureStatusEvent]()

    init(sendDelayNanoseconds: UInt64 = 0) {
        self.sendDelayNanoseconds = sendDelayNanoseconds
    }

    var sequences: [UInt64] {
        lock.lock()
        defer { lock.unlock() }
        return recordedSequences
    }

    var statusEvents: [CaptureStatusEvent] {
        lock.lock()
        defer { lock.unlock() }
        return recordedStatusEvents
    }

    func connect(sessionID: String, sources: [AudioSource]) async throws {}

    func send(_ frame: AudioFrame) async throws {
        if sendDelayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: sendDelayNanoseconds)
        }
        lock.lock()
        recordedSequences.append(frame.sequence)
        lock.unlock()
    }

    func send(_ event: CaptureStatusEvent) async throws {
        lock.lock()
        recordedStatusEvents.append(event)
        lock.unlock()
    }

    func pause(sessionID: String) async {}
    func resume(sessionID: String) async {}
    func disconnect(sessionID: String, reason: String) async {}
}
