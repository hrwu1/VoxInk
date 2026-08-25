import AVFoundation
import Foundation

final class PCMNormalizer {
    let outputSampleRate: Double

    init(outputSampleRate: Double) {
        self.outputSampleRate = outputSampleRate
    }

    func normalize(_ input: AVAudioPCMBuffer) throws -> Data {
        guard input.frameLength > 0 else { return Data() }
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: outputSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            throw CaptureError.audioFormatUnavailable("could not create 16-bit mono output")
        }
        guard let converter = AVAudioConverter(from: input.format, to: outputFormat) else {
            throw CaptureError.audioFormatUnavailable("\(input.format) cannot convert to \(outputFormat)")
        }

        let ratio = outputSampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio)) + 1
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw CaptureError.conversionFailed
        }

        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, outStatus in
            if supplied {
                outStatus.pointee = .endOfStream
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return input
        }
        guard conversionError == nil, status != .error else { throw conversionError ?? CaptureError.conversionFailed }
        guard let audioBuffer = output.audioBufferList.pointee.mBuffers.mData else { return Data() }
        return Data(bytes: audioBuffer, count: Int(output.audioBufferList.pointee.mBuffers.mDataByteSize))
    }
}
