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
    /// The format the IO proc's buffers are read with. Not the tap's declared
    /// format: it carries the tap's channel layout at the rate the device is
    /// measured to be delivering, which is the only rate the audio is really at.
    private var inputFormat: AVAudioFormat?
    /// The tap's own description, kept so a corrected rate can be applied to it
    /// without asking the HAL again.
    private var tapStreamDescription: AudioStreamBasicDescription?
    private var rateEstimator = DeliveredRateEstimator()
    /// The output device the current aggregate was built around. The aggregate
    /// is pinned to it, so when the default moves the tap has to be rebuilt.
    private var builtWithOutputUID: String?
    private var needsRebuild = false

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
        // Every channel needs the monitor now, not just the application-scoped
        // ones: it is also what notices the output device changing underneath
        // a whole-system tap.
        startMonitor()
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
        // The tap declares a format, but the audio arrives through the
        // aggregate device, clocked by the output device — and the two do
        // disagree: a tap here declared 48 kHz while a Bluetooth headset
        // delivered 44.1 kHz, and 16 kHz in its call profile. The nominal rate
        // is the better opening guess of the two, and `DeliveredRateEstimator`
        // corrects whichever of them is wrong within about a second.
        var format = streamDescription
        if let nominalRate: Float64 = try? CoreAudioSupport.scalarProperty(
            aggregate, selector: kAudioDevicePropertyNominalSampleRate
        ), nominalRate > 0 {
            format.mSampleRate = nominalRate
        }
        guard let tapFormat = AVAudioFormat(streamDescription: &format) else {
            AudioHardwareDestroyAggregateDevice(aggregate)
            AudioHardwareDestroyProcessTap(tap)
            throw CaptureError.audioFormatUnavailable("tap format \(streamDescription.mFormatID)")
        }

        var procID: AudioDeviceIOProcID?
        // The block reads the current format rather than capturing one, so a
        // corrected rate takes effect on the next buffer.
        status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregate, queue) { [weak self] _, inputData, inputTime, _, _ in
            self?.handle(bufferList: inputData, inputTime: inputTime)
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
            tapStreamDescription = format
            inputFormat = tapFormat
            builtWithOutputUID = outputUID
            needsRebuild = false
            // Evidence gathered from the previous device says nothing about
            // this one.
            rateEstimator.reset()
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
            // A callback already in flight finds no format and returns rather
            // than reading a buffer against a torn-down device's layout.
            inputFormat = nil
            tapStreamDescription = nil
            builtWithOutputUID = nil
            rateEstimator.reset()
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

    /// Audio is being lost from the moment the output device changes until the
    /// tap is rebuilt on the new one, so the check that costs two property
    /// reads runs four times a second. Listing every audio process, and
    /// retrying a rebuild that is waiting on hardware to appear, do not get
    /// better for being asked more often, so they stay on the slow tick.
    private static let monitorInterval: UInt64 = 250_000_000
    private static let slowTickEvery = 8

    /// One task, one tick at a time, so no two rebuilds ever overlap.
    private func startMonitor() {
        let task = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.monitorInterval)
                guard !Task.isCancelled, let self, !self.isStopping else { return }
                tick += 1
                await self.monitorTick(slowTick: tick % Self.slowTickEvery == 0)
            }
        }
        withLock { reconnectTask = task }
    }

    private func monitorTick(slowTick: Bool) async {
        if withLock({ needsRebuild }) {
            if slowTick { await performRebuild(reason: "an earlier rebuild failed") }
            return
        }
        if await followOutputDeviceChange() { return }
        if slowTick { await refreshProcesses() }
    }

    /// Rebuild when the user sends their audio somewhere else.
    ///
    /// The aggregate device names its output device at creation and keeps it,
    /// so after a switch it is clocked by — or waiting on — a device the user
    /// has left. Unplugged headphones would otherwise end the capture silently.
    private func followOutputDeviceChange() async -> Bool {
        guard let built = withLock({ builtWithOutputUID }) else { return false }
        guard let current = try? CoreAudioSupport.defaultOutputDeviceUID(), current != built else {
            return false
        }
        await emitStatus(
            level: .info,
            code: "output_device_changed",
            message: "The default output device changed; rebuilding tap \(channel.streamID) on it."
        )
        await performRebuild(reason: "the output device changed")
        return true
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
        } catch {
            await emitStatus(level: .error, code: "application_reconnect_failed", message: error.localizedDescription)
            return
        }
        await performRebuild(reason: "the tapped processes changed")
    }

    /// Rebuild, and leave the session running if it fails.
    ///
    /// There may be no output device at all for a moment — the last one
    /// unplugged, none chosen yet — and that is a wait, not the end of a
    /// recording. The monitor retries until one is there.
    private func performRebuild(reason: String) async {
        do {
            try await rebuildTap(allowMissingProcesses: true)
        } catch {
            withLock { needsRebuild = true }
            await emitStatus(
                level: .warning,
                code: "tap_rebuild_failed",
                message: "Could not rebuild tap \(channel.streamID) after \(reason): \(error.localizedDescription). Retrying."
            )
        }
    }

    // MARK: - Audio delivery

    private func handle(bufferList: UnsafePointer<AudioBufferList>, inputTime: UnsafePointer<AudioTimeStamp>) {
        guard let format = withLock({ inputFormat }) else { return }
        guard let input = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: bufferList, deallocator: nil),
              input.frameLength > 0
        else { return }
        // The buffer's frame count never depended on the assumed rate — only
        // how long those frames are held to last did, which is the whole
        // defect — so the measurement is sound even while the rate is wrong.
        trackDeliveredRate(frameCount: Int(input.frameLength), timestamp: inputTime.pointee, assumedRate: format.sampleRate)
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

    /// Measure what the device is delivering, and convert at that rate instead
    /// when it disagrees with what the format claims.
    private func trackDeliveredRate(frameCount: Int, timestamp: AudioTimeStamp, assumedRate: Double) {
        // A device that does not stamp its buffers still has a clock we can
        // read; reading it here is no worse than the timestamp would have been.
        let hostTime = timestamp.mFlags.contains(.hostTimeValid) ? timestamp.mHostTime : mach_absolute_time()
        let corrected = withLock { () -> Double? in
            rateEstimator.observe(
                frameCount: frameCount,
                hostSeconds: Self.seconds(fromHostTime: hostTime),
                assumedRate: assumedRate
            )
        }
        guard let corrected else { return }
        let adopted: Bool = withLock {
            guard var description = tapStreamDescription else { return false }
            description.mSampleRate = corrected
            guard let format = AVAudioFormat(streamDescription: &description) else { return false }
            tapStreamDescription = description
            inputFormat = format
            return true
        }
        guard adopted else { return }
        let streamID = channel.streamID
        Task { [weak self] in
            await self?.emitStatus(
                level: .warning,
                code: "tap_rate_corrected",
                message: "Tap \(streamID) is delivering \(Int(corrected)) Hz, not the \(Int(assumedRate)) Hz its format declared; converting at the measured rate."
            )
        }
    }

    private static let timebase: mach_timebase_info_data_t = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return info
    }()

    private static func seconds(fromHostTime hostTime: UInt64) -> TimeInterval {
        let info = timebase
        guard info.denom != 0 else { return 0 }
        return Double(hostTime) * Double(info.numer) / Double(info.denom) / 1_000_000_000
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
