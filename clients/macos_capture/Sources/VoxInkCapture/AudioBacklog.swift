import Foundation

/// Frame start times on the session timeline that never step backwards.
///
/// A frame's start is its capture time minus its duration. After an audio
/// overload the HAL delivers the late buffers in a burst, so that arithmetic
/// puts one frame inside the previous one, and a consumer that tiles audio by
/// time (live speakers) refuses the overlap. Clamping a start to the previous
/// frame's end keeps the frames contiguous; the next on-time frame lands
/// after that end anyway, so the drift is bounded by the burst itself.
public struct StreamClock: Sendable {
    private var lastEndMilliseconds = [String: Double]()

    public init() {}

    public func startMilliseconds(
        streamID: String, capturedAtMilliseconds: Double, durationMilliseconds: Double
    ) -> Double {
        let nominal = max(0, capturedAtMilliseconds - durationMilliseconds)
        return max(nominal, lastEndMilliseconds[streamID] ?? 0)
    }

    /// Record that a frame ending at `endMilliseconds` was delivered.
    public mutating func advance(streamID: String, toMilliseconds endMilliseconds: Double) {
        lastEndMilliseconds[streamID] = max(lastEndMilliseconds[streamID] ?? 0, endMilliseconds)
    }
}

/// Frames captured while the service could not take them, in capture order.
///
/// Bounded in bytes: past the capacity the oldest frames are handed back to
/// the caller, which writes them to the spool file, so memory stays flat no
/// matter how long the service is away and nothing is dropped.
public struct AudioBacklog: Sendable {
    public let capacityBytes: Int
    public private(set) var frames = [AudioFrame]()
    public private(set) var byteCount = 0

    public init(capacityBytes: Int) {
        self.capacityBytes = max(1, capacityBytes)
    }

    public var isEmpty: Bool { frames.isEmpty }

    /// Keep `frame` for later delivery; returns the oldest frames that no
    /// longer fit. Eviction frees a quarter of the capacity at a time so a
    /// long outage spools in batches rather than one frame per frame.
    public mutating func append(_ frame: AudioFrame) -> [AudioFrame] {
        frames.append(frame)
        byteCount += frame.pcmS16LE.count
        guard byteCount > capacityBytes else { return [] }
        let target = capacityBytes - capacityBytes / 4
        var evictedBytes = 0
        var count = 0
        while count < frames.count - 1, byteCount - evictedBytes > target {
            evictedBytes += frames[count].pcmS16LE.count
            count += 1
        }
        let evicted = Array(frames[0..<count])
        frames.removeFirst(count)
        byteCount -= evictedBytes
        return evicted
    }

    public mutating func drain() -> [AudioFrame] {
        let drained = frames
        frames.removeAll()
        byteCount = 0
        return drained
    }
}

/// A WAV file holding audio the service never received.
///
/// The last resort of a recording: whatever cannot be delivered live is at
/// least on disk, beside the transcript, in a form any transcriber reads. The
/// RIFF sizes are patched every couple of seconds so a helper that dies
/// mid-write leaves a readable file, not a header claiming zero samples.
public final class AudioSpool {
    public let url: URL
    public let sampleRate: Int
    public let channels: Int
    public private(set) var dataBytes = 0
    public private(set) var isClosed = false
    private let handle: FileHandle
    private var bytesSinceHeader = 0
    private static let headerLength = 44

    public init(directory: URL, streamID: String, sampleRate: Int, channels: Int = 1) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var index = 1
        var candidate: URL
        repeat {
            candidate = directory.appendingPathComponent(
                "\(streamID)-spool-\(String(format: "%03d", index)).wav"
            )
            index += 1
        } while FileManager.default.fileExists(atPath: candidate.path)
        guard FileManager.default.createFile(
            atPath: candidate.path, contents: nil, attributes: [.posixPermissions: 0o600]
        ) else {
            throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: candidate.path])
        }
        url = candidate
        self.sampleRate = sampleRate
        self.channels = channels
        handle = try FileHandle(forWritingTo: candidate)
        try handle.write(contentsOf: Self.header(dataBytes: 0, sampleRate: sampleRate, channels: channels))
    }

    public var durationSeconds: Double {
        Double(dataBytes) / Double(sampleRate * channels * 2)
    }

    public func append(_ pcm: Data) throws {
        guard !isClosed else { return }
        try handle.write(contentsOf: pcm)
        dataBytes += pcm.count
        bytesSinceHeader += pcm.count
        if bytesSinceHeader >= sampleRate * channels * 2 * 2 {
            try patchHeader()
            try handle.seekToEnd()
        }
    }

    public func close() throws {
        guard !isClosed else { return }
        isClosed = true
        try patchHeader()
        try handle.close()
    }

    private func patchHeader() throws {
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: Self.header(dataBytes: dataBytes, sampleRate: sampleRate, channels: channels))
        bytesSinceHeader = 0
    }

    static func header(dataBytes: Int, sampleRate: Int, channels: Int) -> Data {
        var data = Data(capacity: headerLength)
        func append(_ string: String) { data.append(contentsOf: Array(string.utf8)) }
        func append(_ value: UInt32) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        func append(_ value: UInt16) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        let payload = UInt32(clamping: dataBytes)
        let blockAlign = UInt16(channels * 2)
        append("RIFF")
        append(UInt32(clamping: 36 + Int(payload)))
        append("WAVE")
        append("fmt ")
        append(UInt32(16))
        append(UInt16(1))
        append(UInt16(channels))
        append(UInt32(sampleRate))
        append(UInt32(sampleRate) * UInt32(blockAlign))
        append(blockAlign)
        append(UInt16(16))
        append("data")
        append(payload)
        return data
    }
}
