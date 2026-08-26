import AVFoundation
import Foundation

/// Converts capture buffers to the service's 16 kHz mono signed 16-bit PCM.
///
/// The converter is retained across buffers so the resampler keeps its filter
/// state: rebuilding it per buffer restarts the filter at every boundary and
/// leaves audible discontinuities in the 48 kHz to 16 kHz path.
///
/// Each capture source owns its own normalizer and drives it from a single
/// callback thread — the microphone tap or the ScreenCaptureKit sample queue —
/// so no locking is required here.
final class PCMNormalizer {
    let outputSampleRate: Double

    private let outputFormat: AVAudioFormat
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?

    init(outputSampleRate: Double) {
        self.outputSampleRate = outputSampleRate
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: outputSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            preconditionFailure("16-bit mono PCM at \(outputSampleRate) Hz is always representable")
        }
        self.outputFormat = outputFormat
    }

    func normalize(_ input: AVAudioPCMBuffer) throws -> Data {
        guard input.frameLength > 0 else { return Data() }
        let converter = try converter(for: input.format)

        let ratio = outputSampleRate / input.format.sampleRate
        // Slack covers resampler latency: a primed converter may emit slightly
        // more than this buffer's share once earlier input has been buffered.
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio)) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw CaptureError.conversionFailed
        }

        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, outStatus in
            if supplied {
                // `noDataNow` pauses this call without ending the stream, which
                // is what lets the same converter serve the next buffer.
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return input
        }
        guard conversionError == nil, status != .error else {
            // A failed conversion leaves the converter's state undefined.
            self.converter = nil
            converterInputFormat = nil
            throw conversionError ?? CaptureError.conversionFailed
        }
        guard output.frameLength > 0,
              let audioBuffer = output.audioBufferList.pointee.mBuffers.mData
        else { return Data() }
        // Size from the produced frame count rather than the buffer's capacity.
        let bytesPerFrame = Int(outputFormat.streamDescription.pointee.mBytesPerFrame)
        return Data(bytes: audioBuffer, count: Int(output.frameLength) * bytesPerFrame)
    }

    private func converter(for inputFormat: AVAudioFormat) throws -> AVAudioConverter {
        if let converter, let converterInputFormat, converterInputFormat == inputFormat {
            return converter
        }
        guard let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw CaptureError.audioFormatUnavailable("\(inputFormat) cannot convert to \(outputFormat)")
        }
        self.converter = converter
        converterInputFormat = inputFormat
        return converter
    }
}
