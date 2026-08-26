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
    private let callbackQueue = DispatchQueue(label: "dev.voxink.capture.system-audio")
    private var stream: SCStream?
    private var reconnectTask: Task<Void, Never>?
    private var sequence: UInt64 = 0
    private var activeProcessIDs = Set<pid_t>()
    private var stopping = false

    init(selection: SystemAudioSelection, sessionID: String, sampleRate: Int, sink: AudioFrameSink) {
        self.selection = selection
        self.sessionID = sessionID
        self.sink = sink
        normalizer = PCMNormalizer(outputSampleRate: Double(sampleRate))
    }

    func start() async throws {
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
            throw CaptureError.screenRecordingPermissionDenied
        }
        stopping = false
        try await rebuildStream(allowMissingApplications: true)
        if case .applications = selection {
            startReconnectMonitor()
        }
    }

    func stop() async {
        stopping = true
        reconnectTask?.cancel()
        reconnectTask = nil
        if let stream {
            try? await stream.stopCapture()
        }
        stream = nil
        activeProcessIDs = []
    }

    private func rebuildStream(allowMissingApplications: Bool) async throws {
        if let stream {
            try? await stream.stopCapture()
            self.stream = nil
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
            activeProcessIDs = []
        case .applications(let bundleIdentifiers):
            let applications = content.applications.filter {
                bundleIdentifiers.contains($0.bundleIdentifier) && $0.processID != ProcessInfo.processInfo.processIdentifier
            }
            guard !applications.isEmpty else {
                activeProcessIDs = []
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
            activeProcessIDs = Set(applications.map(\.processID))
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

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: callbackQueue)
        self.stream = stream
        try await stream.startCapture()
        let sourceDescription = selectedIDs?.joined(separator: ", ") ?? "all system audio"
        await emitStatus(level: .info, code: "system_capture_started", message: "Capturing \(sourceDescription).")
    }

    private func startReconnectMonitor() {
        reconnectTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, let self, !self.stopping else { return }
                await self.refreshSelectedApplications()
            }
        }
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
            guard currentPIDs != activeProcessIDs else { return }
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
            let data = try normalizer.normalize(input)
            guard !data.isEmpty else { return }
            sequence += 1
            let appIDs: [String]?
            if case .applications(let identifiers) = selection { appIDs = identifiers.sorted() } else { appIDs = nil }
            let frame = AudioFrame(
                sessionID: sessionID,
                streamID: "system-1",
                source: .system,
                sequence: sequence,
                timestampNanoseconds: DispatchTime.now().uptimeNanoseconds,
                sampleRate: Int(normalizer.outputSampleRate),
                pcmS16LE: data,
                applicationBundleIdentifiers: appIDs
            )
            Task { try? await sink.send(frame) }
        } catch {
            Task { await emitStatus(level: .warning, code: "system_frame_dropped", message: error.localizedDescription) }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard !stopping else { return }
        Task {
            await emitStatus(level: .error, code: "system_capture_stopped", message: error.localizedDescription)
            try? await rebuildStream(allowMissingApplications: true)
        }
    }

    private func emitStatus(level: CaptureStatusEvent.Level, code: String, message: String) async {
        let event = CaptureStatusEvent(sessionID: sessionID, source: .system, level: level, code: code, message: message)
        try? await sink.send(event)
    }

    private static func pcmBuffer(from sampleBuffer: CMSampleBuffer) throws -> AVAudioPCMBuffer {
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

        let sources = UnsafeMutableAudioBufferListPointer(sourceList)
        let destinations = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        guard sources.count == destinations.count else {
            throw CaptureError.audioFormatUnavailable("source and destination buffer counts differ")
        }
        for (source, destination) in zip(sources, destinations) {
            guard let sourceData = source.mData, let destinationData = destination.mData else { continue }
            memcpy(destinationData, sourceData, min(Int(source.mDataByteSize), Int(destination.mDataByteSize)))
        }
        pcmBuffer.frameLength = frameCount
        return pcmBuffer
    }
}
