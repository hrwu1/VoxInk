import AVFoundation
import CoreAudio
import Foundation

/// System-audio capture through Core Audio process taps (macOS 14.2+).
///
/// A tap intercepts what a process sends to the output device, before the
/// device volume or mute is applied, so it keeps delivering audio while the
/// speakers are muted. It needs only the "System Audio Recording Only"
/// permission, not Screen Recording, and it can be scoped to one process, so
/// selected-application capture opens one tap per application and each one is
/// its own labeled stream ("channel"). ScreenCaptureKit remains the fallback
/// where taps are unavailable.
@available(macOS 14.2, *)
final class ProcessTapCapture {
    /// What one tap listens to and which stream it feeds.
    struct Channel: Equatable, Sendable {
        /// Nil means every process except this one.
        let bundleIdentifier: String?
        let name: String?
        let streamID: String
    }

    private let channel: Channel
    private let sessionID: String
    private let sink: AudioFrameSink
    private let normalizer: PCMNormalizer
    private let frames: AudioFrameQueue
    private let queue = DispatchQueue(label: "dev.voxink.capture.process-tap")

    private let lock = NSLock()
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var tappedProcesses = Set<AudioObjectID>()
    private var sequence: UInt64 = 0
    private var capturedFrameCount: UInt64 = 0
    private var sourceNonzeroByteCount: UInt64 = 0
    private var sourceByteCount: UInt64 = 0
    private var peakAmplitude = 0
    private var stopping = false
    private var reconnectTask: Task<Void, Never>?

    init(channel: Channel, sessionID: String, sampleRate: Int, sink: AudioFrameSink) {
        self.channel = channel
        self.sessionID = sessionID
        self.sink = sink
        normalizer = PCMNormalizer(outputSampleRate: Double(sampleRate))
        frames = AudioFrameQueue(sink: sink, sessionID: sessionID, source: .system)
    }

    var droppedFrameCount: Int { frames.droppedFrameCount }

    func start() async throws {
        withLock { stopping = false }
        frames.start()
        do {
            try await rebuildTap(allowMissingProcesses: true)
        } catch {
            await frames.finishAndDrain()
            throw error
        }
        if channel.bundleIdentifier != nil {
            startReconnectMonitor()
        }
    }

    /// The tap stays open across a pause so no permission is re-requested;
    /// only delivery stops.
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
        teardown()
        await frames.finishAndDrain()
        let summary = withLock {
            (capturedFrameCount, peakAmplitude, sourceNonzeroByteCount, sourceByteCount)
        }
        await emitStatus(
            level: summary.0 > 0 && summary.2 > 0 ? .info : .warning,
            code: "tap_capture_summary",
            message: "Tap \(channel.streamID): \(summary.0) frames, output peak \(summary.1) of 32768, raw nonzero bytes \(summary.2)/\(summary.3)."
        )
    }

    // MARK: - Tap lifecycle

    private func rebuildTap(allowMissingProcesses: Bool) async throws {
        teardown()
        let description: CATapDescription
        if let bundle = channel.bundleIdentifier {
            let processes = try CoreAudioSupport.audioProcesses().filter {
                $0.bundleIdentifier == bundle && $0.pid != ProcessInfo.processInfo.processIdentifier
            }
            guard !processes.isEmpty else {
                withLock { tappedProcesses = [] }
                if allowMissingProcesses {
                    await emitStatus(
                        level: .warning,
                        code: "applications_waiting",
                        message: "Waiting for \(channel.name ?? bundle) to play audio."
                    )
                    return
                }
                throw CaptureError.noSelectedApplications([bundle])
            }
            description = CATapDescription(
                stereoMixdownOfProcesses: processes.map(\.objectID)
            )
            withLock { tappedProcesses = Set(processes.map(\.objectID)) }
        } else {
            let own = CoreAudioSupport.processObject(forPID: ProcessInfo.processInfo.processIdentifier)
            description = CATapDescription(
                stereoGlobalTapButExcludeProcesses: own.map { [$0] } ?? []
            )
        }
        description.uuid = UUID()
        description.name = "VoxInk \(channel.streamID)"
        description.muteBehavior = .unmuted
        description.isPrivate = true

        var tap = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(description, &tap)
        guard status == noErr, tap != kAudioObjectUnknown else {
            throw CaptureError.tapUnavailable("process tap could not be created (status \(status))")
        }

        let outputUID: String
        do {
            outputUID = try CoreAudioSupport.defaultOutputDeviceUID()
        } catch {
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.tapUnavailable("no default output device: \(error.localizedDescription)")
        }
        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey: "VoxInk Tap \(channel.streamID)",
            kAudioAggregateDeviceUIDKey: "dev.voxink.tap.\(UUID().uuidString)",
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: true,
                kAudioSubTapUIDKey: description.uuid.uuidString,
            ]],
        ]
        var aggregate = AudioObjectID(kAudioObjectUnknown)
        status = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregate)
        guard status == noErr, aggregate != kAudioObjectUnknown else {
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.tapUnavailable("aggregate device could not be created (status \(status))")
        }

        let streamDescription: AudioStreamBasicDescription
        do {
            streamDescription = try CoreAudioSupport.scalarProperty(tap, selector: kAudioTapPropertyFormat)
        } catch {
            AudioHardwareDestroyAggregateDevice(aggregate)
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.tapUnavailable("tap format unavailable: \(error.localizedDescription)")
        }
        var format = streamDescription
        guard let tapFormat = AVAudioFormat(streamDescription: &format) else {
            AudioHardwareDestroyAggregateDevice(aggregate)
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.audioFormatUnavailable("tap format \(streamDescription.mFormatID)")
        }

        var procID: AudioDeviceIOProcID?
        status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregate, queue) { [weak self] _, inputData, _, _, _ in
            self?.handle(bufferList: inputData, format: tapFormat)
        }
        guard status == noErr, let procID else {
            AudioHardwareDestroyAggregateDevice(aggregate)
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.tapUnavailable("IO proc could not be installed (status \(status))")
        }
        status = AudioDeviceStart(aggregate, procID)
        guard status == noErr else {
            AudioDeviceDestroyIOProcID(aggregate, procID)
            AudioHardwareDestroyAggregateDevice(aggregate)
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.tapUnavailable("tap device could not start (status \(status))")
        }
        withLock {
            tapID = tap
            aggregateID = aggregate
            ioProcID = procID
        }
        await emitStatus(
            level: .info,
            code: "tap_capture_started",
            message: "Capturing \(channel.name ?? channel.bundleIdentifier ?? "all system audio") through a Core Audio process tap."
        )
    }

    private func teardown() {
        let (tap, aggregate, proc) = withLock { () -> (AudioObjectID, AudioObjectID, AudioDeviceIOProcID?) in
            let state = (tapID, aggregateID, ioProcID)
            tapID = AudioObjectID(kAudioObjectUnknown)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
            ioProcID = nil
            return state
        }
        if aggregate != kAudioObjectUnknown {
            if let proc {
                AudioDeviceStop(aggregate, proc)
                AudioDeviceDestroyIOProcID(aggregate, proc)
            }
            AudioHardwareDestroyAggregateDevice(aggregate)
        }
        if tap != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tap)
        }
    }

    private func startReconnectMonitor() {
        let task = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, let self, !self.isStopping else { return }
                await self.refreshProcesses()
            }
        }
        withLock { reconnectTask = task }
    }

    private func refreshProcesses() async {
        guard let bundle = channel.bundleIdentifier else { return }
        do {
            let current = Set(try CoreAudioSupport.audioProcesses().filter {
                $0.bundleIdentifier == bundle && $0.pid != ProcessInfo.processInfo.processIdentifier
            }.map(\.objectID))
            guard current != withLock({ tappedProcesses }) else { return }
            await emitStatus(
                level: .info,
                code: "applications_changed",
                message: "\(channel.name ?? bundle) processes changed; rebuilding the tap."
            )
            try await rebuildTap(allowMissingProcesses: true)
        } catch {
            await emitStatus(level: .error, code: "application_reconnect_failed", message: error.localizedDescription)
        }
    }

    // MARK: - Audio delivery

    private func handle(bufferList: UnsafePointer<AudioBufferList>, format: AVAudioFormat) {
        guard let input = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: bufferList, deallocator: nil),
              input.frameLength > 0
        else { return }
        do {
            let activity = SystemAudioCapture.pcmByteActivity(input)
            let data = try normalizer.normalize(input)
            guard !data.isEmpty else { return }
            let sequence = withLock { () -> UInt64 in
                self.sequence += 1
                capturedFrameCount += 1
                sourceNonzeroByteCount += UInt64(activity.nonzero)
                sourceByteCount += UInt64(activity.total)
                peakAmplitude = max(peakAmplitude, SystemAudioCapture.pcmPeakAmplitude(data))
                return self.sequence
            }
            frames.submit(AudioFrame(
                sessionID: sessionID,
                streamID: channel.streamID,
                source: .system,
                sequence: sequence,
                timestampNanoseconds: DispatchTime.now().uptimeNanoseconds,
                sampleRate: Int(normalizer.outputSampleRate),
                pcmS16LE: data,
                applicationBundleIdentifiers: channel.bundleIdentifier.map { [$0] },
                channel: channel.bundleIdentifier,
                channelName: channel.name
            ))
        } catch {
            Task { await emitStatus(level: .warning, code: "system_frame_dropped", message: error.localizedDescription) }
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
}
