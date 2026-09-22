import AVFoundation
import CoreAudio
import Foundation

final class MicrophoneCapture {
    /// Replaced, not restarted, when the input device changes: a new engine
    /// reads the device the system has now, while a restarted one keeps the
    /// input node's old format and refuses to start (`kAudioUnitErr_FormatNotSupported`).
    private var engine = AVAudioEngine()
    private let normalizer: PCMNormalizer
    private let sessionID: String
    private let sink: AudioFrameSink
    private let queue: AudioFrameQueue
    private let deviceUID: String?
    private let holdGain: Bool
    private let streamID = "microphone-1"
    private let lock = NSLock()
    private let listenerQueue = DispatchQueue(label: "dev.voxink.capture.microphone-devices")
    private var sequence: UInt64 = 0
    private var running = false
    private var reattaching = false
    private var needsReattach = false
    /// The device the engine's input unit is pinned to. The engine is not left
    /// on the system's default-device aggregate because, in this process, that
    /// aggregate kept delivering zeros from the previous device after a switch.
    private var attachedDevice = AudioDeviceID(kAudioObjectUnknown)
    private var configurationObserver: NSObjectProtocol?
    private var defaultInputListener: AudioObjectPropertyListenerBlock?
    private var monitor: Task<Void, Never>?
    private var gainMonitor: InputGainMonitor?
    private var activity = StreamActivityTracker()

    init(sessionID: String, sampleRate: Int, sink: AudioFrameSink, deviceUID: String? = nil, holdGain: Bool = false) {
        self.sessionID = sessionID
        self.sink = sink
        self.deviceUID = deviceUID
        self.holdGain = holdGain
        normalizer = PCMNormalizer(outputSampleRate: Double(sampleRate))
        queue = AudioFrameQueue(sink: sink, sessionID: sessionID, source: .microphone)
    }

    var droppedFrameCount: Int { queue.droppedFrameCount }

    func start() async throws {
        let granted: Bool
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            granted = true
        case .notDetermined:
            granted = await AVCaptureDevice.requestAccess(for: .audio)
        default:
            granted = false
        }
        guard granted else { throw CaptureError.microphonePermissionDenied }
        // A named device that is not there is an error at start; later on it
        // is something to wait for.
        if let deviceUID, CoreAudioSupport.deviceID(forUID: deviceUID) == nil {
            throw CaptureError.microphoneDeviceUnavailable(deviceUID)
        }

        let engine = withLock { self.engine }
        queue.start()
        do {
            _ = try attach(engine)
        } catch {
            await queue.finishAndDrain()
            throw error
        }
        withLock { running = true }
        startFollowingDeviceChanges(on: engine)
    }

    /// Pin `engine`'s input to the microphone this session wants, install the
    /// tap, start the engine, and start watching that device's input volume.
    ///
    /// No tap format is named on purpose: the tap then takes whatever the
    /// node delivers, and `PCMNormalizer` follows each buffer's own format.
    /// Naming a format the node has moved away from raises an Objective-C
    /// exception that Swift cannot catch — seen live when a reattach after a
    /// device switch asked for the previous device's rate.
    private func attach(_ engine: AVAudioEngine) throws -> (device: AudioDeviceID, format: AVAudioFormat) {
        let input = engine.inputNode
        let device = try wantedDevice()
        try Self.select(device: device, on: input)
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CaptureError.audioFormatUnavailable("microphone returned \(format)")
        }
        input.installTap(onBus: 0, bufferSize: 2_048, format: nil) { [weak self] buffer, _ in
            self?.handle(buffer: buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
        withLock { attachedDevice = device }
        watchGain(of: device)
        return (device, format)
    }

    /// The named device while it exists, otherwise the system default.
    private func wantedDevice() throws -> AudioDeviceID {
        if let deviceUID, let named = CoreAudioSupport.deviceID(forUID: deviceUID) {
            return named
        }
        do {
            return try CoreAudioSupport.defaultInputDeviceID()
        } catch {
            throw CaptureError.audioFormatUnavailable("no default input device: \(error.localizedDescription)")
        }
    }

    // MARK: - Following the device

    /// Reattach when the input device changes underneath us.
    ///
    /// Switching headsets mid-session changes the input as well as the output,
    /// and the engine answers that by stopping and discarding the installed
    /// tap. Nothing throws and nothing arrives: the microphone would simply go
    /// quiet for the rest of the recording. Two prompts cover it: the engine's
    /// configuration-change notification (the pinned device changed format or
    /// went away) and the system's default-input property (the user chose
    /// another microphone). A 2 s poll backs both up, because a reattach can
    /// find no usable input yet — the old device gone, the new one not ready —
    /// and because a notification can be missed while one is in progress.
    private func startFollowingDeviceChanges(on engine: AVAudioEngine) {
        let observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: nil
        ) { [weak self] _ in
            Task { await self?.reattach(reason: "the audio input changed") }
        }
        let previous = withLock { () -> NSObjectProtocol? in
            let previous = configurationObserver
            configurationObserver = observer
            return previous
        }
        if let previous { NotificationCenter.default.removeObserver(previous) }
        guard withLock({ monitor == nil }) else { return }

        if deviceUID == nil {
            let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                Task { await self?.reattach(reason: "the default input device changed") }
            }
            withLock { defaultInputListener = block }
            CoreAudioSupport.addListener(
                CoreAudioSupport.systemObject, selector: kAudioHardwarePropertyDefaultInputDevice,
                queue: listenerQueue, block: block
            )
        }
        let poll = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, let self, self.withLock({ self.running }) else { return }
                await self.reattach(reason: "the microphone was still detached")
            }
        }
        withLock { monitor = poll }
    }

    /// Whether the engine is no longer capturing the microphone it should be.
    private var needsAttention: Bool {
        let (engine, attached, pending) = withLock { (self.engine, attachedDevice, needsReattach) }
        if pending || !engine.isRunning { return true }
        guard let wanted = try? wantedDevice() else { return false }
        return wanted != attached
    }

    private func reattach(reason: String) async {
        // One reattach at a time: the two notifications and the poll can all
        // fire for the same change, and whichever runs first does the work.
        let proceed = withLock { () -> Bool in
            guard running, !reattaching else { return false }
            reattaching = true
            return true
        }
        guard proceed else { return }
        defer { withLock { reattaching = false } }
        guard needsAttention else { return }

        let fresh = AVAudioEngine()
        let old = withLock { () -> AVAudioEngine in
            let old = engine
            engine = fresh
            return old
        }
        old.inputNode.removeTap(onBus: 0)
        old.stop()
        do {
            let (device, format) = try attach(fresh)
            withLock { needsReattach = false }
            startFollowingDeviceChanges(on: fresh)
            let name = CoreAudioSupport.deviceName(device) ?? "device \(device)"
            await emitStatus(
                level: .info,
                code: "microphone_reattached",
                message: "Microphone capture reattached to \(name) at \(Int(format.sampleRate)) Hz after \(reason)."
            )
        } catch CaptureError.audioFormatUnavailable {
            withLock { needsReattach = true }
            await emitStatus(
                level: .warning,
                code: "microphone_waiting",
                message: "No usable audio input after \(reason); waiting for one."
            )
        } catch {
            withLock { needsReattach = true }
            await emitStatus(
                level: .warning,
                code: "microphone_reattach_failed",
                message: "Could not restart microphone capture after \(reason): \(error.localizedDescription). Retrying."
            )
        }
    }

    // MARK: - Input volume

    /// Watch the input volume of the device being captured. Each reattach
    /// starts over on the new device, whose level at that moment is the one
    /// worth holding.
    private func watchGain(of device: AudioDeviceID) {
        let previous = withLock { () -> InputGainMonitor? in
            let previous = gainMonitor
            gainMonitor = nil
            return previous
        }
        previous?.stop()
        let name = CoreAudioSupport.deviceName(device) ?? "device \(device)"
        guard let monitor = InputGainMonitor(device: device, hold: holdGain, handler: { [weak self] event in
            Task { await self?.report(event, deviceName: name) }
        }) else { return }
        withLock { gainMonitor = monitor }
        monitor.start()
    }

    private func report(_ event: InputGainMonitor.Event, deviceName name: String) async {
        func percent(_ value: Double) -> String { "\(Int((value * 100).rounded()))%" }
        switch event {
        case .changed(let from, let to, let warning):
            await emitStatus(
                level: warning ? .warning : .info,
                code: warning ? "microphone_gain_dropped" : "microphone_gain_changed",
                message: warning
                    ? "\(name) input volume fell from \(percent(from)) to \(percent(to)), probably by another app adjusting it; speech at this level may not be transcribed."
                    : "\(name) input volume changed from \(percent(from)) to \(percent(to))."
            )
        case .recovered(let to):
            await emitStatus(
                level: .info,
                code: "microphone_gain_changed",
                message: "\(name) input volume is back at \(percent(to))."
            )
        case .restored(let from, let to):
            await emitStatus(
                level: .info,
                code: "microphone_gain_restored",
                message: "\(name) input volume was lowered to \(percent(from)); put back to \(percent(to))."
            )
        case .restoreFailed(let from, let to, let status):
            await emitStatus(
                level: .warning,
                code: "microphone_gain_hold_failed",
                message: "\(name) input volume fell to \(percent(from)) and could not be put back to \(percent(to)) (status \(status))."
            )
        case .holdExhausted(let at, let reference):
            await emitStatus(
                level: .warning,
                code: "microphone_gain_hold_failed",
                message: "\(name) input volume keeps being lowered (now \(percent(at)), was \(percent(reference))); no longer putting it back. Turn off automatic microphone volume in the call app."
            )
        }
    }

    // MARK: - Plumbing

    private func emitStatus(level: CaptureStatusEvent.Level, code: String, message: String) async {
        try? await sink.send(CaptureStatusEvent(
            sessionID: sessionID, source: .microphone, level: level, code: code, message: message
        ))
    }

    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    /// Point the engine's input unit at one device.
    private static func select(device: AudioDeviceID, on input: AVAudioInputNode) throws {
        guard let unit = input.audioUnit else {
            throw CaptureError.audioFormatUnavailable("microphone input unit is unavailable")
        }
        var device = device
        let status = AudioUnitSetProperty(
            unit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &device,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard status == noErr else {
            throw CaptureError.microphoneDeviceUnavailable("device \(device) (status \(status))")
        }
    }

    /// Stop delivering audio while the session is paused. Frames captured
    /// before the pipeline closes are still sent, so the utterance in progress
    /// is finalized rather than truncated.
    func pause() async {
        await queue.finishAndDrain()
    }

    func resume() {
        queue.start()
    }

    func stop() async {
        // Cleared first, so a configuration change arriving during teardown
        // does not reinstall the tap that is being removed.
        let (observer, listener, monitor, gain, engine) = withLock {
            () -> (NSObjectProtocol?, AudioObjectPropertyListenerBlock?, Task<Void, Never>?, InputGainMonitor?, AVAudioEngine) in
            running = false
            let state = (configurationObserver, defaultInputListener, self.monitor, gainMonitor, self.engine)
            configurationObserver = nil
            defaultInputListener = nil
            self.monitor = nil
            gainMonitor = nil
            return state
        }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        if let listener {
            CoreAudioSupport.removeListener(
                CoreAudioSupport.systemObject, selector: kAudioHardwarePropertyDefaultInputDevice,
                queue: listenerQueue, block: listener
            )
        }
        monitor?.cancel()
        gain?.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        await queue.finishAndDrain()
    }

    private func handle(buffer: AVAudioPCMBuffer) {
        do {
            let data = try normalizer.normalize(buffer)
            guard !data.isEmpty else { return }
            let peak = SystemAudioCapture.pcmPeakAmplitude(data)
            let now = Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000
            let (sequence, transition) = withLock { () -> (UInt64, StreamActivityTracker.Transition?) in
                self.sequence += 1
                return (self.sequence, activity.observe(peak: peak, atSeconds: now))
            }
            if let transition {
                let line = withLock { activity.describe(transition, streamID: streamID) }
                Task { await emitStatus(level: .info, code: line.code, message: line.message) }
            }
            queue.submit(AudioFrame(
                sessionID: sessionID,
                streamID: streamID,
                source: .microphone,
                sequence: sequence,
                timestampNanoseconds: DispatchTime.now().uptimeNanoseconds,
                sampleRate: Int(normalizer.outputSampleRate),
                pcmS16LE: data
            ))
        } catch {
            // Capture continues after an isolated conversion failure. A host app
            // can replace this with structured diagnostics through its logger.
        }
    }
}
