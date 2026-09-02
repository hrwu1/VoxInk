import AVFoundation
import CoreAudio
import Foundation

final class MicrophoneCapture {
    private let engine = AVAudioEngine()
    private let normalizer: PCMNormalizer
    private let sessionID: String
    private let queue: AudioFrameQueue
    private let deviceUID: String?
    private let streamID = "microphone-1"
    private let lock = NSLock()
    private var sequence: UInt64 = 0

    init(sessionID: String, sampleRate: Int, sink: AudioFrameSink, deviceUID: String? = nil) {
        self.sessionID = sessionID
        self.deviceUID = deviceUID
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

        let input = engine.inputNode
        if let deviceUID {
            try Self.select(deviceUID: deviceUID, on: input)
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CaptureError.audioFormatUnavailable("microphone returned \(format)")
        }
        queue.start()
        input.installTap(onBus: 0, bufferSize: 2_048, format: format) { [weak self] buffer, _ in
            self?.handle(buffer: buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            await queue.finishAndDrain()
            throw error
        }
    }

    /// Point the engine's input unit at one device. The engine otherwise
    /// follows the system default input, which is what most people want and
    /// exactly what someone with a USB microphone plugged in does not.
    private static func select(deviceUID: String, on input: AVAudioInputNode) throws {
        guard var device = CoreAudioSupport.deviceID(forUID: deviceUID) else {
            throw CaptureError.microphoneDeviceUnavailable(deviceUID)
        }
        guard let unit = input.audioUnit else {
            throw CaptureError.audioFormatUnavailable("microphone input unit is unavailable")
        }
        let status = AudioUnitSetProperty(
            unit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &device,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard status == noErr else {
            throw CaptureError.microphoneDeviceUnavailable("\(deviceUID) (status \(status))")
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
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        await queue.finishAndDrain()
    }

    private func handle(buffer: AVAudioPCMBuffer) {
        do {
            let data = try normalizer.normalize(buffer)
            guard !data.isEmpty else { return }
            lock.lock()
            sequence += 1
            let sequence = self.sequence
            lock.unlock()
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
