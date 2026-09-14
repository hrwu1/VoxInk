import XCTest
@testable import VoxInkCapture

private func frame(_ sequence: UInt64, bytes: Int = 640, streamID: String = "microphone-1") -> AudioFrame {
    AudioFrame(
        sessionID: "session-1",
        streamID: streamID,
        source: .microphone,
        sequence: sequence,
        timestampNanoseconds: sequence * 20_000_000,
        sampleRate: 16_000,
        pcmS16LE: Data(repeating: UInt8(truncatingIfNeeded: sequence), count: bytes)
    )
}

final class StreamClockTests: XCTestCase {
    func testOnTimeFramesKeepTheirCaptureClock() {
        var clock = StreamClock()
        XCTAssertEqual(0, clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 20, durationMilliseconds: 20))
        clock.advance(streamID: "m", toMilliseconds: 20)
        XCTAssertEqual(20, clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 40, durationMilliseconds: 20))
        clock.advance(streamID: "m", toMilliseconds: 40)
        // A gap (a dropped cycle) is preserved: the frame stays late.
        XCTAssertEqual(100, clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 120, durationMilliseconds: 20))
    }

    func testALateBurstNeverStepsBackIntoThePreviousFrame() {
        var clock = StreamClock()
        clock.advance(streamID: "m", toMilliseconds: 1000)
        // Two buffers delivered 3 ms apart after an overload: the nominal
        // start of the second lands 17 ms inside the first.
        let first = clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 1020, durationMilliseconds: 20)
        XCTAssertEqual(1000, first)
        clock.advance(streamID: "m", toMilliseconds: first + 20)
        let second = clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 1023, durationMilliseconds: 20)
        XCTAssertEqual(1020, second)
        clock.advance(streamID: "m", toMilliseconds: second + 20)
        // The next on-time frame re-syncs with the wall clock.
        XCTAssertEqual(1060, clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 1080, durationMilliseconds: 20))
    }

    func testStreamsHaveIndependentClocks() {
        var clock = StreamClock()
        clock.advance(streamID: "microphone-1", toMilliseconds: 500)
        XCTAssertEqual(0, clock.startMilliseconds(streamID: "system-1", capturedAtMilliseconds: 20, durationMilliseconds: 20))
    }

    func testAnUndeliveredFrameDoesNotMoveTheClock() {
        var clock = StreamClock()
        _ = clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 1000, durationMilliseconds: 20)
        XCTAssertEqual(0, clock.startMilliseconds(streamID: "m", capturedAtMilliseconds: 10, durationMilliseconds: 20))
    }
}

final class AudioBacklogTests: XCTestCase {
    func testFramesComeBackInCaptureOrder() {
        var backlog = AudioBacklog(capacityBytes: 10_000)
        XCTAssertTrue(backlog.isEmpty)
        XCTAssertEqual([], backlog.append(frame(1)))
        XCTAssertEqual([], backlog.append(frame(2)))
        XCTAssertEqual(1280, backlog.byteCount)
        XCTAssertEqual([1, 2], backlog.drain().map(\.sequence))
        XCTAssertTrue(backlog.isEmpty)
        XCTAssertEqual(0, backlog.byteCount)
    }

    func testOverflowEvictsTheOldestQuarterAndKeepsTheRest() {
        var backlog = AudioBacklog(capacityBytes: 4 * 640)
        for sequence in 1...4 { XCTAssertEqual([], backlog.append(frame(UInt64(sequence)))) }
        // Past capacity the oldest frames go, down to three quarters of it,
        // so a long outage spools in batches rather than one frame per frame.
        XCTAssertEqual([1, 2], backlog.append(frame(5)).map(\.sequence))
        XCTAssertEqual([3, 4, 5], backlog.frames.map(\.sequence))
        XCTAssertEqual(3 * 640, backlog.byteCount)
        XCTAssertEqual([], backlog.append(frame(6)))
        XCTAssertEqual([3, 4, 5, 6], backlog.frames.map(\.sequence))
    }

    func testAFrameLargerThanTheCapacityIsStillKept() {
        var backlog = AudioBacklog(capacityBytes: 100)
        XCTAssertEqual([], backlog.append(frame(1)))
        XCTAssertEqual([1], backlog.append(frame(2)).map(\.sequence))
        XCTAssertEqual([2], backlog.frames.map(\.sequence))
    }
}

final class AudioSpoolTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voxink-spool-\(UUID().uuidString)", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func header(of url: URL) throws -> (riffSize: UInt32, channels: UInt16, rate: UInt32, dataSize: UInt32, total: Int) {
        let data = try Data(contentsOf: url)
        func u32(_ offset: Int) -> UInt32 { data[offset..<offset + 4].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }.littleEndian }
        func u16(_ offset: Int) -> UInt16 { data[offset..<offset + 2].withUnsafeBytes { $0.loadUnaligned(as: UInt16.self) }.littleEndian }
        XCTAssertEqual("RIFF", String(decoding: data[0..<4], as: UTF8.self))
        XCTAssertEqual("WAVE", String(decoding: data[8..<12], as: UTF8.self))
        XCTAssertEqual("fmt ", String(decoding: data[12..<16], as: UTF8.self))
        XCTAssertEqual("data", String(decoding: data[36..<40], as: UTF8.self))
        XCTAssertEqual(1, u16(20))
        XCTAssertEqual(16, u16(34))
        return (u32(4), u16(22), u32(24), u32(40), data.count)
    }

    func testWritesAReadableMonoWavBesideTheTranscript() throws {
        let spool = try AudioSpool(directory: directory, streamID: "microphone-1", sampleRate: 16_000)
        XCTAssertEqual("microphone-1-spool-001.wav", spool.url.lastPathComponent)
        try spool.append(Data(repeating: 1, count: 640))
        try spool.append(Data(repeating: 2, count: 640))
        XCTAssertEqual(0.04, spool.durationSeconds, accuracy: 0.0001)
        try spool.close()
        let header = try header(of: spool.url)
        XCTAssertEqual(1, header.channels)
        XCTAssertEqual(16_000, header.rate)
        XCTAssertEqual(1280, header.dataSize)
        XCTAssertEqual(36 + 1280, header.riffSize)
        XCTAssertEqual(44 + 1280, header.total)
        let attributes = try FileManager.default.attributesOfItem(atPath: spool.url.path)
        XCTAssertEqual(0o600, (attributes[.posixPermissions] as? Int) ?? 0)
    }

    func testASecondSpoolForTheSameStreamGetsTheNextName() throws {
        let first = try AudioSpool(directory: directory, streamID: "system-1", sampleRate: 16_000)
        try first.close()
        let second = try AudioSpool(directory: directory, streamID: "system-1", sampleRate: 16_000)
        XCTAssertEqual("system-1-spool-002.wav", second.url.lastPathComponent)
        try second.close()
    }

    func testTheHeaderIsPatchedWhileStillOpenSoACrashLeavesAReadableFile() throws {
        let spool = try AudioSpool(directory: directory, streamID: "microphone-1", sampleRate: 16_000)
        // Three seconds of audio crosses the two-second patch interval: the
        // header lags the data by less than two seconds, never by everything.
        for _ in 0..<150 { try spool.append(Data(count: 640)) }
        let open = try header(of: spool.url)
        XCTAssertEqual(UInt32(100 * 640), open.dataSize)
        XCTAssertEqual(44 + 150 * 640, open.total)
        try spool.append(Data(count: 640))
        try spool.close()
        XCTAssertEqual(UInt32(151 * 640), try header(of: spool.url).dataSize)
    }
}
