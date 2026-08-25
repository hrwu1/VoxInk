import AVFoundation
import Foundation

final class MicrophoneCapture {
    private let engine = AVAudioEngine()
    private let normalizer: PCMNormalizer
    private let sessionID: String
    private let sink: AudioFrameSink
    private let streamID = "microphone-1"
    private var sequence: UInt64 = 0

    init(sessionID: String, sampleRate: Int, sink: AudioFrameSink) {
        self.sessionID = sessionID
        self.sink = sink
        normalizer = PCMNormalizer(outputSampleRate: Double(sampleRate))
    }

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
        input.installTap(onBus: 0, bufferSize: 2_048, format: format) { [weak self] buffer, _ in
            self?.handle(buffer: buffer)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    private func handle(buffer: AVAudioPCMBuffer) {
        do {
            let data = try normalizer.normalize(buffer)
            guard !data.isEmpty else { return }
            sequence += 1
            let frame = AudioFrame(
                sessionID: sessionID,
                streamID: streamID,
                source: .microphone,
                sequence: sequence,
                timestampNanoseconds: DispatchTime.now().uptimeNanoseconds,
                sampleRate: Int(normalizer.outputSampleRate),
                pcmS16LE: data
            )
            Task { try? await sink.send(frame) }
        } catch {
            // Capture continues after an isolated conversion failure. A host app
            // can replace this with structured diagnostics through its logger.
        }
    }
}
