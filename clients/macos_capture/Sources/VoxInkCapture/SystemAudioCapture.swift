import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

public struct CapturableApplication: Equatable, Sendable {
    public let name: String
    public let bundleIdentifier: String
    public let processID: pid_t
}

public enum CaptureApplicationCatalog {
    public static func runningApplications() async throws -> [CapturableApplication] {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        return content.applications.compactMap { application in
            guard !application.bundleIdentifier.isEmpty else { return nil }
            return CapturableApplication(
                name: application.applicationName,
                bundleIdentifier: application.bundleIdentifier,
                processID: application.processID
            )
        }.sorted { lhs, rhs in
            if lhs.name == rhs.name { return lhs.processID < rhs.processID }
            return lhs.name.localizedCaseInsensitiveCompare(rhs.name) == .orderedAscending
        }
    }
}

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let selection: SystemAudioSelection
    private let sessionID: String
    private let sink: AudioFrameSink
    private let normalizer: PCMNormalizer
    private let frames: AudioFrameQueue
    private let callbackQueue = DispatchQueue(label: "dev.voxink.capture.system-audio")

    /// ScreenCaptureKit delivers samples and stop callbacks on its own queues
    /// while capture control runs on Swift concurrency's executors, so every
    /// mutable field below is reached from more than one thread.
    private let lock = NSLock()
    private var stream: SCStream?
    private var reconnectTask: Task<Void, Never>?
    private var sequence: UInt64 = 0
    private var capturedFrameCount: UInt64 = 0
    private var sourcePeakAmplitude = 0.0
    private var sourceByteCount: UInt64 = 0
    private var sourceNonzeroByteCount: UInt64 = 0
    private var sourceFormatDescription = "unavailable"
    private var peakAmplitude = 0
    private var activeProcessIDs = Set<pid_t>()
    private var stopping = false

    init(selection: SystemAudioSelection, sessionID: String, sampleRate: Int, sink: AudioFrameSink) {
        self.selection = selection
        self.sessionID = sessionID
        self.sink = sink
        normalizer = PCMNormalizer(outputSampleRate: Double(sampleRate))
        frames = AudioFrameQueue(sink: sink, sessionID: sessionID, source: .system)
    }

    var droppedFrameCount: Int { frames.droppedFrameCount }

    func start() async throws {
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
            throw CaptureError.screenRecordingPermissionDenied
        }
        withLock { stopping = false }
        frames.start()
        do {
            try await rebuildStream(allowMissingApplications: true)
        } catch {
            await frames.finishAndDrain()
            throw error
        }
        if case .applications = selection {
            startReconnectMonitor()
        }
    }

    /// Keep the ScreenCaptureKit stream alive across a pause — tearing it down
    /// would surrender the capture filter and re-prompt for permission — and
    /// close the delivery pipeline instead so no audio reaches the service.
    func pause() async {
        await frames.finishAndDrain()
    }

    func resume() {
        frames.start()
    }

    func stop() async {
        withLock { stopping = true }
        let task = withLock { () -> Task<Void, Never>? in
            let task = reconnectTask
            reconnectTask = nil
            return task
        }
        task?.cancel()
        let stream = withLock { () -> SCStream? in
            let stream = self.stream
            self.stream = nil
            activeProcessIDs = []
            return stream
        }
        if let stream {
            try? await stream.stopCapture()
        }
        await frames.finishAndDrain()
        let summary = withLock {
            (
                capturedFrameCount,
                sourcePeakAmplitude,
                peakAmplitude,
                sourceNonzeroByteCount,
                sourceByteCount,
                sourceFormatDescription
            )
        }
        await emitStatus(
            level: summary.0 > 0 ? .info : .warning,
            code: "system_capture_summary",
            message: "Captured \(summary.0) system audio frames (source peak \(String(format: "%.4f", summary.1)); output peak \(summary.2) of 32768; raw nonzero bytes \(summary.3)/\(summary.4); format \(summary.5))."
        )
    }

    private func rebuildStream(allowMissingApplications: Bool) async throws {
        let existing = withLock { () -> SCStream? in
            let existing = self.stream
            self.stream = nil
            return existing
        }
        if let existing {
            try? await existing.stopCapture()
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw CaptureError.noDisplayAvailable }

        let filter: SCContentFilter
        var selectedIDs: [String]? = nil
        switch selection {
        case .all:
            let ownPID = ProcessInfo.processInfo.processIdentifier
            let ownApplications = content.applications.filter { $0.processID == ownPID }
            filter = SCContentFilter(display: display, excludingApplications: ownApplications, exceptingWindows: [])
            withLock { activeProcessIDs = [] }
        case .applications(let bundleIdentifiers):
            let applications = content.applications.filter {
                bundleIdentifiers.contains($0.bundleIdentifier) && $0.processID != ProcessInfo.processInfo.processIdentifier
            }
            guard !applications.isEmpty else {
                withLock { activeProcessIDs = [] }
                if allowMissingApplications {
                    await emitStatus(
                        level: .warning,
                        code: "applications_waiting",
                        message: "Waiting for selected applications: \(bundleIdentifiers.sorted().joined(separator: ", "))"
                    )
                    return
                }
                throw CaptureError.noSelectedApplications(bundleIdentifiers)
            }
            filter = SCContentFilter(display: display, including: applications, exceptingWindows: [])
            withLock { activeProcessIDs = Set(applications.map(\.processID)) }
            selectedIDs = bundleIdentifiers.sorted()
        }

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 5)
        configuration.width = 2
        configuration.height = 2
        configuration.showsCursor = false

        let newStream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try newStream.addStreamOutput(self, type: .audio, sampleHandlerQueue: callbackQueue)
        withLock { self.stream = newStream }
        do {
            try await newStream.startCapture()
        } catch {
            withLock { if self.stream === newStream { self.stream = nil } }
            throw error
        }
        let sourceDescription = selectedIDs?.joined(separator: ", ") ?? "all system audio"
        await emitStatus(level: .info, code: "system_capture_started", message: "Capturing \(sourceDescription).")
    }

    private func startReconnectMonitor() {
        let task = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, let self, !self.isStopping else { return }
                await self.refreshSelectedApplications()
            }
        }
        withLock { reconnectTask = task }
    }

    private func refreshSelectedApplications() async {
        guard case .applications(let bundleIdentifiers) = selection else { return }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            let currentPIDs = Set(content.applications.compactMap { application -> pid_t? in
                guard bundleIdentifiers.contains(application.bundleIdentifier),
                      application.processID != ProcessInfo.processInfo.processIdentifier
                else { return nil }
                return application.processID
            })
            guard currentPIDs != withLock({ activeProcessIDs }) else { return }
            await emitStatus(level: .info, code: "applications_changed", message: "Selected application processes changed; rebuilding capture filter.")
            try await rebuildStream(allowMissingApplications: true)
        } catch {
            await emitStatus(level: .error, code: "application_reconnect_failed", message: error.localizedDescription)
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        do {
            let input = try Self.pcmBuffer(from: sampleBuffer)
            let sourcePeak = Self.pcmPeakAmplitude(input)
            let sourceActivity = Self.pcmByteActivity(input)
            let data = try normalizer.normalize(input)
            guard !data.isEmpty else { return }
            let sequence = withLock { () -> UInt64 in
                self.sequence += 1
                capturedFrameCount += 1
                sourcePeakAmplitude = max(sourcePeakAmplitude, sourcePeak)
                sourceNonzeroByteCount += UInt64(sourceActivity.nonzero)
                sourceByteCount += UInt64(sourceActivity.total)
                sourceFormatDescription = input.format.description
                peakAmplitude = max(peakAmplitude, Self.pcmPeakAmplitude(data))
                return self.sequence
            }
            let appIDs: [String]?
            if case .applications(let identifiers) = selection { appIDs = identifiers.sorted() } else { appIDs = nil }
            frames.submit(AudioFrame(
                sessionID: sessionID,
                streamID: "system-1",
                source: .system,
                sequence: sequence,
                timestampNanoseconds: DispatchTime.now().uptimeNanoseconds,
                sampleRate: Int(normalizer.outputSampleRate),
                pcmS16LE: data,
                applicationBundleIdentifiers: appIDs
            ))
        } catch {
            Task { await emitStatus(level: .warning, code: "system_frame_dropped", message: error.localizedDescription) }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard !isStopping else { return }
        Task {
            await emitStatus(level: .error, code: "system_capture_stopped", message: error.localizedDescription)
            try? await rebuildStream(allowMissingApplications: true)
        }
    }

    private var isStopping: Bool { withLock { stopping } }

    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    private func emitStatus(level: CaptureStatusEvent.Level, code: String, message: String) async {
        let event = CaptureStatusEvent(sessionID: sessionID, source: .system, level: level, code: code, message: message)
        try? await sink.send(event)
    }

    static func pcmPeakAmplitude(_ data: Data) -> Int {
        guard data.count >= 2 else { return 0 }
        return data.withUnsafeBytes { raw in
            var peak = 0
            for offset in stride(from: 0, to: raw.count - 1, by: 2) {
                let bits = UInt16(raw[offset]) | UInt16(raw[offset + 1]) << 8
                peak = max(peak, abs(Int(Int16(bitPattern: bits))))
            }
            return peak
        }
    }

    static func pcmPeakAmplitude(_ buffer: AVAudioPCMBuffer) -> Double {
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        guard frames > 0, channels > 0 else { return 0 }
        var peak = 0.0
        switch buffer.format.commonFormat {
        case .pcmFormatFloat32:
            guard let data = buffer.floatChannelData else { return 0 }
            for channel in 0..<channels {
                for frame in 0..<frames { peak = max(peak, abs(Double(data[channel][frame]))) }
            }
        case .pcmFormatFloat64:
            for audioBuffer in UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList) {
                guard let raw = audioBuffer.mData else { continue }
                let data = raw.assumingMemoryBound(to: Double.self)
                let sampleCount = Int(audioBuffer.mDataByteSize) / MemoryLayout<Double>.size
                for sample in 0..<sampleCount { peak = max(peak, abs(data[sample])) }
            }
        case .pcmFormatInt16:
            guard let data = buffer.int16ChannelData else { return 0 }
            for channel in 0..<channels {
                for frame in 0..<frames {
                    peak = max(peak, Double(abs(Int(data[channel][frame]))) / 32768.0)
                }
            }
        case .pcmFormatInt32:
            guard let data = buffer.int32ChannelData else { return 0 }
            for channel in 0..<channels {
                for frame in 0..<frames {
                    peak = max(peak, Double(abs(Int64(data[channel][frame]))) / 2_147_483_648.0)
                }
            }
        case .otherFormat:
            return 0
        @unknown default:
            return 0
        }
        return peak
    }

    static func pcmByteActivity(_ buffer: AVAudioPCMBuffer) -> (nonzero: Int, total: Int) {
        let buffers = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        var nonzero = 0
        var total = 0
        for audioBuffer in buffers {
            guard let data = audioBuffer.mData else { continue }
            let bytes = UnsafeRawBufferPointer(
                start: data,
                count: Int(audioBuffer.mDataByteSize)
            )
            total += bytes.count
            nonzero += bytes.reduce(into: 0) { count, byte in
                if byte != 0 { count += 1 }
            }
        }
        return (nonzero, total)
    }

    static func pcmBuffer(from sampleBuffer: CMSampleBuffer) throws -> AVAudioPCMBuffer {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription),
              let format = AVAudioFormat(streamDescription: streamDescription)
        else { throw CaptureError.audioFormatUnavailable("missing CMSampleBuffer format description") }

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else {
            throw CaptureError.audioFormatUnavailable("could not allocate PCM buffer")
        }

        var requiredSize = 0
        var blockBuffer: CMBlockBuffer?
        var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &requiredSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }

        let raw = UnsafeMutableRawPointer.allocate(byteCount: requiredSize, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        let sourceList = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: sourceList,
            bufferListSize: requiredSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }

        // `mutableAudioBufferList` sizes each buffer from `frameLength`, which
        // starts at zero, so the destination has to be sized before the copy:
        // otherwise every memcpy clamps to zero bytes and the buffer stays
        // silent while still reporting the right frame count and format.
        pcmBuffer.frameLength = frameCount
        let sources = UnsafeMutableAudioBufferListPointer(sourceList)
        let destinations = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        guard sources.count == destinations.count else {
            throw CaptureError.audioFormatUnavailable("source and destination buffer counts differ")
        }
        for (source, destination) in zip(sources, destinations) {
            guard let sourceData = source.mData, let destinationData = destination.mData else { continue }
            memcpy(destinationData, sourceData, min(Int(source.mDataByteSize), Int(destination.mDataByteSize)))
        }
        return pcmBuffer
    }
}
