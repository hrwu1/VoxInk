import AVFoundation
import Foundation

final class MicrophoneCapture {
    private let engine = AVAudioEngine()
    private let normalizer: PCMNormalizer
    private let sessionID: String
    private let queue: AudioFrameQueue
    private let streamID = "microphone-1"
    private let lock = NSLock()
    private var sequence: UInt64 = 0

    init(sessionID: String, sampleRate: Int, sink: AudioFrameSink) {
        self.sessionID = sessionID
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
